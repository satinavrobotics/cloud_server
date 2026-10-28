"""Bag analysis routes: results, catalog, artifacts and on-request jobs (docs/bag_analysis_server_2026-10-28.md).

    GET  /api/v1/analyses/catalog                                  catalog()
    GET  /api/v1/analyses/recordings                               recordings()   (table view, paginated)
    GET  /api/v1/recordings/{id}/analyses[?history=1]              results()
    GET  /api/v1/recordings/{id}/analyses/{analyzer}/{profile}/{analyzer_version}/{extractor_version}/artifacts/{path}
                                                                   open_artifact()
    POST /api/v1/recordings/{id}/analyses                          request()      -> 202 / 200
    GET  /api/v1/recordings/{id}/analysis-jobs[?limit=&log=1]      jobs()

DB access is the bag-analysis store (packages/services/bag_analysis/store.py, synchronous psycopg, one short
transaction per call, a statement timeout of FLEET_READ_STATEMENT_TIMEOUT_MS), run in the thread pool. All SQL
stays in the store. The job queue is the worker's: `request` only enqueues a trigger='request' job.

Authorisation: the API has no authentication or roles today (AUDIT_BACKLOG.md B2; packages/api/recording.py
request_actor), so these routes are open exactly like the other write routes (deploy, run archive). The write
(`request`) therefore stores X-Sati-Operator as `requested_by`, unverified, the way the deploy routes do. What
bounds a request: only an existing run recording of part nav with a stored object; one queued/running job per
(recording, trigger, selection) (partial unique index); analyzer ids must be ones that have a stored result.

Everything in a result (notes, table cells, metric names, job errors and log tails) is untrusted text. It is
returned as JSON data only; artifacts are served as attachments with a fixed content type, `nosniff` and a
sandbox CSP, never as HTML.

Catalog limits: the worker does not persist the runner's `--list` manifests, so the catalog is DERIVED from stored
results: analyzers, profiles, versions, metric names. `unit`, `direction`, `headline`, `title` are declared in the
manifest only and are `null` here until the worker stores its catalog (see the doc, "Open points").
"""
import logging
import re
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import psycopg
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

from packages import config
from packages.api import fleet_reads
from packages.services.bag_analysis import store as analysis_store

logger = logging.getLogger("ApiDelegationService.bag_analysis")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ENTRY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}(:[a-z][a-z0-9_]{0,39})?$")   # 'id' or 'id:profile' (contract schema)
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ARTIFACT_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9_./-]+$")           # contract schema, artifacts[].path
DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT = fleet_reads.DEFAULT_LIMIT, 200
MAX_JOBS = 100
MAX_ANALYZER_ENTRIES = 20
MAX_OPERATOR_LEN = 100
CHUNK = 64 * 1024
# What an artifact may be served as. Anything else is application/octet-stream; never html/svg/xml.
SERVED_TYPES = {"application/json", "text/plain", "text/markdown", "text/csv", "image/png", "image/jpeg"}


# --- response models (Pydantic v1) -------------------------------------------------------------------------

class Note(BaseModel):
    code: str
    level: str = "info"
    text: str = Field("", description="Untrusted text; render as plain text.")


class ArtifactInfo(BaseModel):
    name: str
    path: str = Field(..., description="Relative to the analyzer's output directory; also the key of the download route.")
    media_type: str
    size: Optional[int] = None
    sha256: Optional[str] = None
    download_url: Optional[str] = Field(None, description="Server-relative URL of the download route (null when the "
                                                          "object location was not stored).")


class AnalysisResult(BaseModel):
    recording_id: str
    analyzer: str
    analyzer_version: int
    profile: str
    extractor_version: int
    status: str = Field(..., description="ok | warn | n/a | error: the state of the ANALYSIS, not a verdict on the robot")
    metrics: Dict[str, Optional[float]] = {}
    tables: Dict[str, Any] = Field({}, description="name -> {title?, columns[], rows[][]}; cells are untrusted text/numbers")
    notes: List[Note] = []
    producer: Dict[str, Any] = {}
    inputs: Dict[str, Any] = {}
    artifacts: List[ArtifactInfo] = []
    created_at: Optional[str] = None
    latest: bool = Field(True, description="True for the newest row of its (analyzer, profile)")


class RecordingAnalyses(BaseModel):
    recording_id: str
    history: bool
    results: List[AnalysisResult]


class Job(BaseModel):
    id: str
    recording_id: str
    analyzer_ids: Optional[List[str]] = Field(None, description="null = every applicable analyzer")
    trigger: str = Field(..., description="auto | request")
    requested_by: Optional[str] = Field(None, description="Unverified operator label (X-Sati-Operator)")
    status: str = Field(..., description="queued | running | done | failed | cancelled")
    attempts: int = 0
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = Field(None, description="Untrusted text")
    log_tail: Optional[str] = Field(None, description="Only with ?log=1; untrusted text, at most 8000 characters")


class JobList(BaseModel):
    recording_id: str
    items: List[Job]


class AnalysisRequest(BaseModel):
    """`analyzers`: omitted/null = every analyzer that applies; else 'id' or 'id:profile' entries that have a
    stored result somewhere. `force` is not supported: results are insert-only and a job whose results already
    exist is a no-op in the worker, so a re-run of the same key is not possible; an unknown field is a 422."""
    analyzers: Optional[List[str]] = Field(None, max_items=MAX_ANALYZER_ENTRIES)

    class Config:
        extra = "forbid"

    @validator("analyzers", each_item=True)
    def _entry(cls, v):
        if not ENTRY_RE.match(v):
            raise ValueError("expected 'id' or 'id:profile' (lowercase letters, digits, underscore)")
        return v


class RequestOutcome(BaseModel):
    created: bool = Field(..., description="True: a new job was queued (202). False: an identical job is already "
                                           "queued/running and is returned (200).")
    job: Job


class CatalogMetric(BaseModel):
    name: str
    unit: Optional[str] = Field(None, description="Manifest field; null while the catalog is derived")
    direction: Optional[str] = Field(None, description="lower | higher | neutral; null while derived")
    headline: Optional[bool] = Field(None, description="null while derived")
    description: Optional[str] = None


class CatalogAnalyzer(BaseModel):
    analyzer: str
    profile: str
    analyzer_version: int = Field(..., description="Highest stored")
    analyzer_versions: List[int]
    extractor_version: int = Field(..., description="Highest stored")
    recordings: int
    last_result_at: Optional[str] = None
    title: Optional[str] = Field(None, description="Manifest field; null while the catalog is derived")
    metrics: List[CatalogMetric]


class Catalog(BaseModel):
    source: str = Field(..., description="'derived': from stored results. 'declared' is reserved for a stored manifest catalog.")
    declared: bool
    analyzers: List[CatalogAnalyzer]


class LatestHeadline(BaseModel):
    analyzer: str
    profile: str
    analyzer_version: int
    extractor_version: int
    status: str
    metrics: Dict[str, Optional[float]]
    created_at: Optional[str] = None


class LastJob(BaseModel):
    id: str
    trigger: str
    status: str
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


class RecordingRow(BaseModel):
    recording_id: str
    robot_name: str
    group_key: str
    bag_id: Optional[str] = None
    description: Optional[str] = None
    map_id: Optional[str] = None
    size_bytes: Optional[int] = None
    object_present: bool
    created_at: Optional[str] = None
    analysis_state: str = Field(..., description="none | queued | running | failed | analysed. 'failed' = no result "
                                                 "yet and the last job failed; a recording with results is 'analysed' "
                                                 "(status per analyzer in `latest`).")
    latest: List[LatestHeadline]
    last_job: Optional[LastJob] = None


class RecordingPage(BaseModel):
    items: List[RecordingRow]
    next_cursor: Optional[str] = None


# --- store access ------------------------------------------------------------------------------------------

def _iso(value: Any) -> Optional[str]:
    return fleet_reads.iso(value) if value is not None else None


def default_store() -> analysis_store.PgStore:
    ms = config.FLEET_READ_STATEMENT_TIMEOUT_MS
    return analysis_store.PgStore(lambda: analysis_store.connect_from_config(ms))


async def _db(fn: Callable[..., Any], *args: Any) -> Any:
    """A store call in the thread pool; database problems become a readable 503."""
    try:
        return await run_in_threadpool(fn, *args)
    except psycopg.errors.UndefinedTable as exc:
        raise HTTPException(503, "Bag analysis is not available (database migration 20261028_04 not applied)") from exc
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(503, "Query took too long; retry") from exc
    except psycopg.OperationalError as exc:
        logger.warning("bag analysis database unavailable: %s", exc)
        raise HTTPException(503, "Database unavailable") from exc


def check_recording_id(recording_id: str) -> str:
    if not UUID_RE.match(recording_id or ""):
        raise HTTPException(422, [{"loc": ["path", "recording_id"], "msg": "must be a UUID", "type": "value_error"}])
    return recording_id.lower()


def _artifact(row: Dict[str, Any], a: Dict[str, Any]) -> ArtifactInfo:
    path = str(a.get("path", ""))
    url = None
    if a.get("key") and a.get("bucket") and ARTIFACT_PATH_RE.match(path):
        url = "/api/v1/recordings/{}/analyses/{}/{}/{}/{}/artifacts/{}".format(
            row["recording_id"], row["analyzer"], row["profile"], row["analyzer_version"],
            row["extractor_version"], urllib.parse.quote(path, safe="/"))
    size = a.get("size")
    return ArtifactInfo(name=str(a.get("name", "")), path=path, media_type=str(a.get("media_type", "")),
                        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
                        sha256=a.get("sha256") if isinstance(a.get("sha256"), str) else None, download_url=url)


def _metrics(raw: Any) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for k, v in (raw if isinstance(raw, dict) else {}).items():
        out[str(k)] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return out


def result_model(row: Dict[str, Any], latest: bool) -> AnalysisResult:
    notes = [Note(code=str(n.get("code", "")), level=str(n.get("level", "info")), text=str(n.get("text", "")))
             for n in (row.get("notes") or []) if isinstance(n, dict)]
    return AnalysisResult(
        recording_id=row["recording_id"], analyzer=row["analyzer"], analyzer_version=row["analyzer_version"],
        profile=row["profile"], extractor_version=row["extractor_version"], status=row["status"],
        metrics=_metrics(row.get("metrics")),
        tables=row.get("tables") if isinstance(row.get("tables"), dict) else {}, notes=notes,
        producer=row.get("producer") if isinstance(row.get("producer"), dict) else {},
        inputs=row.get("inputs") if isinstance(row.get("inputs"), dict) else {},
        artifacts=[_artifact(row, a) for a in (row.get("artifacts") or []) if isinstance(a, dict)],
        created_at=_iso(row.get("created_at")), latest=latest)


def job_model(row: Dict[str, Any], with_log: bool = False) -> Job:
    return Job(id=row["id"], recording_id=row["recording_id"], analyzer_ids=row.get("analyzer_ids"),
               trigger=row["trigger"], requested_by=row.get("requested_by"), status=row["status"],
               attempts=row.get("attempts") or 0, created_at=_iso(row.get("created_at")),
               started_at=_iso(row.get("started_at")), finished_at=_iso(row.get("finished_at")),
               error=row.get("error"), log_tail=row.get("log_tail") if with_log else None)


class AnalysisApi:
    """The route logic; `store` is a PgStore (tests: a fake with the same methods), `objects` the artifact reader."""

    def __init__(self, store: Any = None, objects: Any = None):
        self.store = store if store is not None else default_store()
        self.objects = objects if objects is not None else MinioArtifacts()

    async def _recording(self, recording_id: str) -> Dict[str, Any]:
        rec = await _db(self.store.get_recording, check_recording_id(recording_id))
        if rec is None:
            raise HTTPException(404, f'Did not find "recording" with id "{recording_id}"')
        return rec

    # -- reads --

    async def catalog(self) -> Catalog:
        rows = await _db(self.store.catalog_summary)
        return Catalog(source="derived", declared=False, analyzers=[
            CatalogAnalyzer(analyzer=r["analyzer"], profile=r["profile"], analyzer_version=r["analyzer_version"],
                            analyzer_versions=list(r["analyzer_versions"]), extractor_version=r["extractor_version"],
                            recordings=r["recordings"], last_result_at=_iso(r.get("last_result_at")),
                            metrics=[CatalogMetric(name=m) for m in r["metrics"]]) for r in rows])

    async def results(self, recording_id: str, history: bool) -> RecordingAnalyses:
        rid = (await self._recording(recording_id))["id"]
        latest = await _db(self.store.latest_results, rid)
        newest = {(r["analyzer"], r["profile"]): (r["analyzer_version"], r["extractor_version"]) for r in latest}
        if history:
            rows = await _db(self.store.results_for_recording, rid)
            items = [result_model(r, newest.get((r["analyzer"], r["profile"])) ==
                                  (r["analyzer_version"], r["extractor_version"])) for r in rows]
        else:
            items = [result_model(r, True) for r in latest]
        return RecordingAnalyses(recording_id=rid, history=history, results=items)

    async def jobs(self, recording_id: str, limit: int, with_log: bool) -> JobList:
        rid = (await self._recording(recording_id))["id"]
        rows = await _db(self.store.jobs_for_recording, rid, limit)
        return JobList(recording_id=rid, items=[job_model(r, with_log) for r in rows])

    async def recordings(self, robot: Optional[str], metrics: Optional[Sequence[str]], cursor: Optional[str],
                         limit: int) -> RecordingPage:
        after = fleet_reads.decode_cursor("analysis_recordings", cursor)
        after_arg = (after[0], str(after[1])) if after else None
        rows = await _db(self.store.recordings_page, limit + 1, robot, after_arg)
        more = len(rows) > limit
        rows = rows[:limit]
        wanted = set(metrics or ())
        items = []
        for r in rows:
            latest = [LatestHeadline(
                analyzer=x["analyzer"], profile=x["profile"], analyzer_version=x["analyzer_version"],
                extractor_version=x["extractor_version"], status=x["status"],
                metrics={k: v for k, v in _metrics(x.get("metrics")).items() if not wanted or k in wanted},
                created_at=_iso(x.get("created_at"))) for x in r["latest"]]
            job = r.get("last_job")
            state = ("running" if job and job["status"] == "running" else
                     "queued" if job and job["status"] == "queued" else
                     "analysed" if latest else
                     "failed" if job and job["status"] == "failed" else "none")
            items.append(RecordingRow(
                recording_id=r["id"], robot_name=r["robot_name"], group_key=r["group_key"], bag_id=r.get("bag_id"),
                description=r.get("description"), map_id=r.get("map_id"), size_bytes=r.get("size_bytes"),
                object_present=bool(r.get("object_present")), created_at=_iso(r.get("created_at")),
                analysis_state=state, latest=latest,
                last_job=LastJob(id=job["id"], trigger=job["trigger"], status=job["status"],
                                 created_at=_iso(job.get("created_at")), finished_at=_iso(job.get("finished_at")),
                                 error=job.get("error")) if job else None))
        nxt = (fleet_reads.encode_cursor("analysis_recordings", rows[-1]["created_at"], rows[-1]["id"])
               if more else None)
        return RecordingPage(items=items, next_cursor=nxt)

    # -- request --

    async def request(self, recording_id: str, body: AnalysisRequest, operator: Optional[str]) -> RequestOutcome:
        rec = await self._recording(recording_id)
        if rec["part"] != "nav" or rec["kind"] != "run":
            raise HTTPException(409, "only run recordings of part nav are analysed")
        if not (rec.get("object_present") and rec.get("size_bytes") is not None):
            raise HTTPException(409, "the recording has no stored bag object")
        entries = sorted(set(body.analyzers)) if body.analyzers else None
        if entries:
            known: Dict[str, set] = {}
            for r in await _db(self.store.catalog_summary):
                known.setdefault(r["analyzer"], set()).add(r["profile"])
            for entry in entries:
                aid, _, profile = entry.partition(":")
                if aid not in known or (profile and profile not in known[aid]):
                    raise HTTPException(422, [{"loc": ["body", "analyzers"], "type": "value_error",
                                               "msg": f"unknown analyzer or profile {entry!r} (no stored result "
                                                      "for it); omit `analyzers` to run everything that applies"}])
        by = (operator or "").strip()[:MAX_OPERATOR_LEN] or None
        job_id = await _db(self.store.enqueue, rec["id"], entries, "request", by)
        if job_id is not None:
            jobs = await _db(self.store.jobs_for_recording, rec["id"], 5)
            job = next((j for j in jobs if j["id"] == job_id), None)
            created = True
        else:
            job = await _db(self.store.active_job, rec["id"], entries, "request")
            created = False
        if job is None:   # the colliding job finished between the two statements: tell the client to retry
            raise HTTPException(409, "the job changed while it was being read; retry")
        return RequestOutcome(created=created, job=job_model(job))

    # -- artifacts --

    async def open_artifact(self, recording_id: str, analyzer: str, profile: str, analyzer_version: int,
                            extractor_version: int, path: str) -> StreamingResponse:
        rid = (await self._recording(recording_id))["id"]
        if not (ID_RE.match(analyzer) and PROFILE_RE.match(profile) and ARTIFACT_PATH_RE.match(path)):
            raise HTTPException(404, "artifact not found")
        rows = await _db(self.store.results_for_recording, rid)
        row = next((r for r in rows if (r["analyzer"], r["profile"], r["analyzer_version"], r["extractor_version"])
                    == (analyzer, profile, analyzer_version, extractor_version)), None)
        art = next((a for a in (row.get("artifacts") or []) if isinstance(a, dict) and a.get("path") == path), None) \
            if row else None
        # the object location comes from the stored list only, and only inside the analysis bucket
        if art is None or not art.get("key") or art.get("bucket") != config.BAG_ANALYSIS_BUCKET \
                or str(art["key"]).startswith("/") or ".." in str(art["key"]).split("/"):
            raise HTTPException(404, "artifact not found")
        try:
            stream, size = await run_in_threadpool(self.objects.open, art["bucket"], art["key"])
        except FileNotFoundError:
            raise HTTPException(404, "artifact object not found in the store") from None
        except Exception:   # noqa: BLE001  the object store is down or misbehaving; no internals to the client
            logger.exception("artifact read failed")
            raise HTTPException(503, "object store unavailable") from None
        media = art.get("media_type") if art.get("media_type") in SERVED_TYPES else "application/octet-stream"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", path.rsplit("/", 1)[-1]) or "artifact"
        headers = {"Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "sandbox; default-src 'none'", "Cache-Control": "private, max-age=3600"}
        if size is not None:
            headers["Content-Length"] = str(size)
        return StreamingResponse(stream, media_type=media, headers=headers)


class MinioArtifacts:
    """Reads one object from the analysis bucket. `open` -> (iterator of byte chunks, size or None);
    FileNotFoundError when the object does not exist. The client is built on first use."""

    def __init__(self, client: Any = None):
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            from minio import Minio
            self._client = Minio(f"{config.MINIO_HOST}:{config.MINIO_PORT}",
                                 access_key=config.require_secret("MINIO_ACCESS_KEY"),
                                 secret_key=config.require_secret("MINIO_SECRET_KEY"),
                                 secure=config.MINIO_SECURE)
        return self._client

    def open(self, bucket: str, key: str):
        from minio.error import S3Error
        client = self._get()
        try:
            resp = client.get_object(bucket, key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchBucket", "NoSuchObject"):
                raise FileNotFoundError(key) from exc
            raise
        size = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None

        def chunks() -> Iterator[bytes]:
            try:
                yield from resp.stream(CHUNK)
            finally:
                resp.close()
                resp.release_conn()
        return chunks(), int(size) if size is not None else None
