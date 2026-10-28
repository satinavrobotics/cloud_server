# Bag analysis, server side: groundwork (2026-10-28)

Status: built and tested without the real analysis image; nothing deployed, no migration run on production, the compose service is not in any
profile in use. Contract: `docs/bag_analysis_contract_2026-10-28.md`. Plan: `web_stack/BAG_ANALYSIS_PIPELINE_PLAN_20261028.md`.

## Tables (migration `20261028_04_bag_analysis`)

- `analysis_jobs(id, recording_id -> recordings, analyzer_ids text[] null, scope, requested_by, trigger auto|request, status, attempts, created_at, started_at, finished_at, error, log_tail)`.
  `analyzer_ids` NULL = every applicable analyzer; entries are `id` or `id:profile`. `scope` ('*' or the sorted entries) exists only so a partial unique
  index can express "one queued/running job per recording + trigger + scope". Finished jobs stay as history.
- `analysis_results(recording_id, analyzer, analyzer_version, profile, extractor_version, status ok|warn|n/a|error, metrics, tables, notes, producer, inputs, artifacts, envelope, created_at)`,
  PK = the five key columns. **Insert only**: a conflict keeps the stored row, except that a stored `error` row is superseded by a re-run's result (decision: an error
  must not block the fix). `envelope` is the validated result.json verbatim; the other jsonb columns are its parts. Differences from the envelope in the columns:
  `artifacts` entries gain `bucket` and `key` (where the file went), `inputs` gains `server_passed` (which optional inputs the server handed the runner), `notes`
  gains a server note `extract.not_decodable` when the extractor reported undecodable topics.
- view `analysis_latest`: per (recording, analyzer, profile) the highest analyzer_version, then extractor_version, then newest.

## Declared catalog (migration `20261028_05_analysis_catalog`)

`analysis_catalog(analyzer, analyzer_version, decl_hash, title, owner, description, profiles text[], metrics jsonb, extractor_version, analysis_image, tool_git_sha, first_seen_at, last_seen_at)`,
PK `(analyzer, analyzer_version, decl_hash)`; `decl_hash` = sha256 of the canonical JSON of the normalised metrics declaration
(`{name: {unit, direction, description, headline}}`, headline defaults false). The worker (`catalog.py`, wrapped around the executor in `main.build_worker`) stores the
`run_analysis.py --list` manifests at startup (also without auto mode) and whenever a later `list_analyzers()` returns a different catalog. Same key seen again: `last_seen_at`
moves and title / owner / description / profiles / extractor_version / analysis_image / tool_git_sha are refreshed. A changed metrics declaration for the same analyzer + version is a
NEW row (the old one stays). Manifest text is untrusted: names re-checked against the schema patterns, unit / direction enums, strings bounded (title/owner 200, description 2000, metric
description 1000, 200 metrics, 50 profiles, 100 analyzers) and stripped of control characters; a manifest that fails is skipped whole and logged. A store failure is logged and
swallowed (retried at the next listing), a failing startup listing is logged and the loop goes on. `--list` may carry a top-level `tool_git_sha` (stored if hex). Downgrade drops the table.

## Configuration (`packages/config.py`, section "Bag analysis"; all defaults safe)

`BAG_ANALYSIS_ENABLED` (false: the service idles), `BAG_ANALYSIS_AUTO` (false: the scheduler enqueues nothing), `BAG_ANALYSIS_SCRATCH_DIR` (/scratch),
`BAG_ANALYSIS_RUN_ANALYSIS` (/opt/navstack/sati_benchmark/analysis/run_analysis.py), `BAG_ANALYSIS_BUCKET` (analysis), `BAG_ANALYSIS_POLL_S` (30), `BAG_ANALYSIS_SCHEDULE_EVERY_S` (300),
`BAG_ANALYSIS_JOB_TIMEOUT_S` (7200), `BAG_ANALYSIS_MEMORY_MB` (0 = no RLIMIT_AS), `BAG_ANALYSIS_STUCK_AFTER_S` (10800), `BAG_ANALYSIS_MAX_ATTEMPTS` (3),
`BAG_ANALYSIS_FAILED_RETRY_AFTER_S` (86400), `BAG_ANALYSIS_DISK_MARGIN_BYTES` (2 GiB), `BAG_ANALYSIS_MAX_OBJECT_BYTES` (30 GiB), `BAG_ANALYSIS_MAX_UNTAR_BYTES` (40 GiB),
`BAG_ANALYSIS_MAX_TAR_MEMBERS` (2000), `BAG_ANALYSIS_MAX_ENVELOPE_BYTES` (256 KiB), `BAG_ANALYSIS_MAX_ARTIFACT_BYTES` (200 MiB).
Compose only: `BAG_ANALYSIS_IMAGE`, `BAG_ANALYSIS_BASE_IMAGE`, `BAG_ANALYSIS_SCRATCH_VOLUME`, `MEM_BAG_ANALYSIS` (8g), `CPUS_BAG_ANALYSIS` (2).

## Job lifecycle

1. Scheduler (auto only) or `scheduler.request(...)` / the API route `POST /api/v1/recordings/{id}/analyses` (on request) enqueues. Candidates: run recordings, part `nav`, object present. Analyzers that apply
   (`applies_to`: description_regex on sidecar description / group_key, bag_id_regex on the sidecar bag_id; `topics_all` is left to the runner, which answers n/a) and have no
   result for the current key (analyzer, version, profile `default`, extractor_version) go in one job. A recording with an active job, or a job (auto OR on request) that failed within the retry window, is skipped.
2. `claim_next`: oldest queued -> running (attempts + 1), `FOR UPDATE SKIP LOCKED`.
3. Worker: entries already analysed are dropped; object missing / over the hard limit / free scratch < 2 x size + margin -> job failed. The tar is streamed from the object store into
   `<scratch>/job-XXXX/bag` through `safe_tar` (only regular files and directories; no absolute names, `..`, links, devices; member, byte and stream caps; O_EXCL|O_NOFOLLOW), then made read-only.
   `<bag>/params` is passed as `--params` when it exists (old bags have none: normal).
4. Runner (`run_analysis.py --bag --out --bag-id --only id:profile,... [--params]`) writes `OUT/<analyzer>/<profile>/result.json` (+ artifacts). Assumption to confirm: that location.
5. Ingest per entry: `contract.check_result`; failure, a missing result.json or unreadable output is stored as an `error` result carrying the reason (never dropped). Valid results: artifacts uploaded, row inserted.
   The runner's own `extractor_version` is accepted as reported.
6. Job `done` (an analyzer `error` is a result, the job is still done) or `failed` (timeout, runner exit not 0/1, extraction failed (3), unsafe tar, upload error, crash) with error and the last 8000 characters of the runner log.
   Scratch is removed in `finally`. SIGTERM kills the runner and releases the job to the queue (attempt not counted, the error text is cleared). Startup sweep: removes `job-*` scratch dirs, re-queues jobs running longer than the limit, fails them once attempts are exhausted.

## Storage layout

Bucket `analysis` (created on first upload), key `<robot>/<bag_id>/<analyzer>/<profile>/v<analyzer_version>_x<extractor_version>/<path>`; `<robot>/<bag_id>` is the recording's `storage_key`.
The version segment is an addition to the plan's layout: results are never overwritten, so their files must not be either.

## Deliberately not built

the client panel, a Docker-socket executor, the backfill run of the 91 existing bags, any change to `satibot_orchestrator` or navstack, building the analysis image,
a health endpoint / heartbeat for the service, retention of artifacts, `parts` support in `applies_to` (the schema has none; only part `nav` is analysed).

## Tests

```
python -m pytest tests/unit/bag_analysis -q -o addopts=""        # 93 tests, no Docker (needs jsonschema; skipped without it)
docker run -d --name ba-test -p 127.0.0.1:55455:5432 -e POSTGRES_PASSWORD=t -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
BAG_ANALYSIS_TEST_DSN="host=127.0.0.1 port=55455 dbname=mission user=postgres password=t" \
  python -m pytest tests/integration/test_bag_analysis_postgres.py -q -o addopts=""   # migration up/down/up, store SQL, concurrent claim
docker rm -f ba-test
```

```
API tests (need the pinned pydantic 1 / fastapi, so on the pinned image, not the host venv):
./scripts/run_unit_tests_pinned.sh tests/unit/test_bag_analysis_api.py
```

## HTTP API (added 2026-10-28; `packages/api/bag_analysis.py`, routes in `packages/api/main.py`)

Served by the API delegation service (port 8000, `/api/v1`). Data comes from the store above (sync psycopg in the thread pool, statement timeout
`FLEET_READ_STATEMENT_TIMEOUT_MS`), artifacts from MinIO (bucket `BAG_ANALYSIS_BUCKET`) through the API's own MinIO credentials. The worker is not involved in reads.

**Auth.** The API has no authentication or roles today (AUDIT_BACKLOG B2, `recording.request_actor`); every route, including the POST, is open like the deploy and
run-archive write routes. There is no role to choose and none was added. The POST stores `X-Sati-Operator` (trimmed to 100 chars) as `requested_by`, UNVERIFIED, the way
the deploy routes do. When auth arrives, the POST belongs with the other write actions (deploy / archive), the GETs with the other reads. Bounds on the unauthenticated POST:
the recording must exist, be a run of part `nav` with a stored object (else 409); one queued/running job per (recording, `request`, selection); named analyzers must have a stored result (else 422).

### Route table

| Method | Path | Request | Response | Errors |
|---|---|---|---|---|
| GET | `/api/v1/analyses/catalog` | none | `Catalog` (stored results merged with stored declarations) | 503 |
| GET | `/api/v1/analyses/recordings` | query `robot`, `metrics` (comma list), `cursor`, `limit` (1-200, default 50) | `RecordingPage` | 422 (cursor, limit), 503 |
| GET | `/api/v1/recordings/{id}/analyses` | query `history` (bool, default false) | `RecordingAnalyses` | 404 unknown recording, 422 id not a UUID, 503 |
| GET | `/api/v1/recordings/{id}/analyses/{analyzer}/{profile}/{analyzer_version}/{extractor_version}/artifacts/{path}` | none | the file (attachment) | 404 (unknown recording / result / path not in the stored list / object gone), 503 object store |
| POST | `/api/v1/recordings/{id}/analyses` | JSON `{"analyzers": ["id" \| "id:profile", ...]}` (body and field optional) | 202 `RequestOutcome` (`created: true`) or 200 (`created: false`, an identical job is active) | 404, 409 (not part nav / not run / no object), 422 (bad entry, unknown analyzer or profile, more than 20 entries, any other field such as `force`), 503 |
| GET | `/api/v1/recordings/{id}/analysis-jobs` | query `limit` (1-100, default 20), `log` (bool) | `JobList` | 404, 422, 503 |

All errors are `{"detail": ...}` (string, or the FastAPI list for 422). 503 means the database or object store is unreachable or the migration is not applied; its text never carries internals.
`{id}` is the `recordings.id` UUID. Timestamps are ISO-8601 UTC. Unknown fields in responses may appear later; ignore them.

### Models (Pydantic v1 in `packages/api/bag_analysis.py`)

```
RecordingAnalyses { recording_id, history: bool, results: [AnalysisResult] }
AnalysisResult    { recording_id, analyzer, analyzer_version:int, profile, extractor_version:int,
                    status: "ok"|"warn"|"n/a"|"error",           # state of the ANALYSIS, not a verdict on the robot
                    metrics: {name: number|null},                # null = not computable for this bag
                    tables: {name: {title?, columns:[str], rows:[[str|number|bool|null]]}},
                    notes: [{code, level: "info"|"warn"|"error", text}],
                    producer: {...}, inputs: {...},               # as stored (tool_git_sha, stack_build, present, server_passed ...)
                    artifacts: [{name, path, media_type, size?, sha256?, download_url|null}],
                    created_at, latest: bool }                    # latest: newest row of its (analyzer, profile)
Catalog           { source: "declared"|"derived", declared: bool,   # source "declared" when any stored declaration is used; declared true when EVERY analyzer row has one
                    analyzers: [{analyzer, profile, analyzer_version, analyzer_versions:[int], extractor_version,
                                 recordings:int, last_result_at|null, title|null, owner|null, description|null,
                                 declared: bool, declared_version:int|null, declared_last_seen_at|null, profiles:[str],
                                 metrics: [{name, unit|null, direction|null, headline|null, description|null, declared: bool}]}] }
```
Catalog rules: rows are (analyzer, profile) from stored results; the declaration used is the one of the row's `analyzer_version`, else the newest declared version of that analyzer
(newest-seen declaration per version); it applies only when the manifest has that profile. `metrics` = stored names UNION declared names; a declared metric carries `unit` (s m rad share count ratio m/s none),
`direction` (`lower_is_better` | `higher_is_better` | `neutral`, as in the manifest), `headline`, `description`, `declared: true`; others stay null / `declared: false`. Analyzer profiles that are declared but
have no stored result are listed with `recordings: 0`, empty `analyzer_versions`, `last_result_at: null`, and can be named in the POST `analyzers`. Fields added in this change: `owner`, `description`, `declared`,
`declared_version`, `declared_last_seen_at`, `profiles`, metric `declared`; the previously null fields are only filled, never changed otherwise.
```
Job               { id, recording_id, analyzer_ids: [str]|null, trigger: "auto"|"request", requested_by|null,
                    status: "queued"|"running"|"done"|"failed"|"cancelled", attempts:int,
                    created_at, started_at|null, finished_at|null, error|null, log_tail|null /* only with ?log=1 */ }
JobList           { recording_id, items: [Job] }                  # newest first
RequestOutcome    { created: bool, job: Job }
RecordingPage     { items: [RecordingRow], next_cursor: str|null }
RecordingRow      { recording_id, robot_name, group_key, bag_id|null, description|null, map_id|null, size_bytes|null,
                    object_present: bool, created_at,
                    analysis_state: "none"|"queued"|"running"|"failed"|"analysed",
                    latest: [{analyzer, profile, analyzer_version, extractor_version, status, metrics, created_at}],
                    last_job: {id, trigger, status, created_at, finished_at, error}|null }
```
`analysis_state`: `running` / `queued` from the newest job; else `analysed` when any result exists; else `failed` when the newest job failed; else `none`.
`RecordingPage.items[].latest[].metrics` holds all metrics, or only those named in `?metrics=` (the table-column use: `?metrics=lost_s,unjustified_s`).
The recordings list covers run recordings of part `nav` (the ones the worker analyses), newest first by (created_at, id); `next_cursor` is opaque, pass it back unchanged with the same filters.

### How a client should use it

- Panel for a recording: `GET .../analyses` (tiles from `metrics`, `status` badge, `notes`, `tables`, artifact links from `download_url`), and `GET .../analysis-jobs?limit=1` while a job may be active (poll every 5-10 s while `queued`/`running`; the worker polls the queue every `BAG_ANALYSIS_POLL_S` = 30 s, a job takes about a minute for a 5 GB bag).
- "Analyse" button: `POST .../analyses` with no body. 202 -> job queued; 200 `created:false` -> already queued/running, show that job. A job whose results already exist ends `done` in under a second with nothing run (`log_tail` says "nothing to run"); that is the idempotent path for "result is up to date" (the API cannot judge it itself, it does not know the runner's current versions). A `failed` job is shown with its `error`; the scheduler will not retry the recording for `BAG_ANALYSIS_FAILED_RETRY_AFTER_S` (24 h), any trigger, but an explicit POST is always allowed.
- Table column: `GET /api/v1/analyses/recordings?metrics=<headline names>&limit=50`, follow `next_cursor`.
- Rendering rule: every string in these responses (note text, table cells, metric names, job `error`, `log_tail`, artifact names) is untrusted. Render as text, never as HTML or markdown. Artifacts are downloads, not pages.
- Tiles without code: names come from `Catalog.analyzers[].metrics`; until `declared` is true, unit / direction / headline are null and the client must fall back to the metric name and a plain number (see open points).

### Artifact download

The path after `/artifacts/` must equal the `path` of an entry in the stored artifact list of exactly that result (analyzer, profile, analyzer_version, extractor_version); the object key and bucket come from that stored entry (written by the worker), never from the request, and the bucket must be `BAG_ANALYSIS_BUCKET`.
Streamed through the API (64 KiB chunks, no presigned URL, so MinIO need not be reachable by the client). `Content-Type` is the stored `media_type` only for `application/json`, `text/plain`, `text/markdown`,
`text/csv`, `image/png`, `image/jpeg`; anything else (html, svg, xml, unknown) is `application/octet-stream`. Always `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'`. `Content-Length` is set from the object.

### Open points of the API

- **The catalog is declared once the worker has run.** Until a worker has listed the runner at least once (table empty) the catalog is derived from stored results as before. Analyzers and profiles from a declaration
  can be named in the POST even without a stored result.
- `force` (re-run an existing key) is not supported: results are insert-only and the worker drops entries that already have a result for the current key. A re-run happens when the analyzer or extractor version changes, or when the stored result is an `error`.
- The POST cannot tell "no analyzer applies" (the applicability rules live in the manifests); such a job ends `done` with nothing run.
- No authentication (see above). `requested_by` is unverified.
- Reads open one short DB connection per call (the store's style), not the API's pooled connection.
