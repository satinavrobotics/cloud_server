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

## Configuration (`packages/config.py`, section "Bag analysis"; all defaults safe)

`BAG_ANALYSIS_ENABLED` (false: the service idles), `BAG_ANALYSIS_AUTO` (false: the scheduler enqueues nothing), `BAG_ANALYSIS_SCRATCH_DIR` (/scratch),
`BAG_ANALYSIS_RUN_ANALYSIS` (/opt/navstack/sati_benchmark/analysis/run_analysis.py), `BAG_ANALYSIS_BUCKET` (analysis), `BAG_ANALYSIS_POLL_S` (30), `BAG_ANALYSIS_SCHEDULE_EVERY_S` (300),
`BAG_ANALYSIS_JOB_TIMEOUT_S` (7200), `BAG_ANALYSIS_MEMORY_MB` (0 = no RLIMIT_AS), `BAG_ANALYSIS_STUCK_AFTER_S` (10800), `BAG_ANALYSIS_MAX_ATTEMPTS` (3),
`BAG_ANALYSIS_FAILED_RETRY_AFTER_S` (86400), `BAG_ANALYSIS_DISK_MARGIN_BYTES` (2 GiB), `BAG_ANALYSIS_MAX_OBJECT_BYTES` (30 GiB), `BAG_ANALYSIS_MAX_UNTAR_BYTES` (40 GiB),
`BAG_ANALYSIS_MAX_TAR_MEMBERS` (2000), `BAG_ANALYSIS_MAX_ENVELOPE_BYTES` (256 KiB), `BAG_ANALYSIS_MAX_ARTIFACT_BYTES` (200 MiB).
Compose only: `BAG_ANALYSIS_IMAGE`, `BAG_ANALYSIS_BASE_IMAGE`, `BAG_ANALYSIS_SCRATCH_VOLUME`, `MEM_BAG_ANALYSIS` (8g), `CPUS_BAG_ANALYSIS` (2).

## Job lifecycle

1. Scheduler (auto only) or `scheduler.request(...)` (on request; no API route yet) enqueues. Candidates: run recordings, part `nav`, object present. Analyzers that apply
   (`applies_to`: description_regex on sidecar description / group_key, bag_id_regex on the sidecar bag_id; `topics_all` is left to the runner, which answers n/a) and have no
   result for the current key (analyzer, version, profile `default`, extractor_version) go in one job. A recording with an active job, or an auto job that failed within the retry window, is skipped.
2. `claim_next`: oldest queued -> running (attempts + 1), `FOR UPDATE SKIP LOCKED`.
3. Worker: entries already analysed are dropped; object missing / over the hard limit / free scratch < 2 x size + margin -> job failed. The tar is streamed from the object store into
   `<scratch>/job-XXXX/bag` through `safe_tar` (only regular files and directories; no absolute names, `..`, links, devices; member, byte and stream caps; O_EXCL|O_NOFOLLOW), then made read-only.
   `<bag>/params` is passed as `--params` when it exists (old bags have none: normal).
4. Runner (`run_analysis.py --bag --out --bag-id --only id:profile,... [--params]`) writes `OUT/<analyzer>/<profile>/result.json` (+ artifacts). Assumption to confirm: that location.
5. Ingest per entry: `contract.check_result`; failure, a missing result.json or unreadable output is stored as an `error` result carrying the reason (never dropped). Valid results: artifacts uploaded, row inserted.
   The runner's own `extractor_version` is accepted as reported.
6. Job `done` (an analyzer `error` is a result, the job is still done) or `failed` (timeout, runner exit not 0/1, extraction failed (3), unsafe tar, upload error, crash) with error and the last 8000 characters of the runner log.
   Scratch is removed in `finally`. SIGTERM kills the runner and releases the job to the queue (attempt not counted). Startup sweep: removes `job-*` scratch dirs, re-queues jobs running longer than the limit, fails them once attempts are exhausted.

## Storage layout

Bucket `analysis` (created on first upload), key `<robot>/<bag_id>/<analyzer>/<profile>/v<analyzer_version>_x<extractor_version>/<path>`; `<robot>/<bag_id>` is the recording's `storage_key`.
The version segment is an addition to the plan's layout: results are never overwritten, so their files must not be either.

## Deliberately not built

API routes (list/get results, run), the client panel, a Docker-socket executor, the backfill run of the 91 existing bags, any change to `satibot_orchestrator` or navstack, building the analysis image,
a health endpoint / heartbeat for the service, retention of artifacts, `parts` support in `applies_to` (the schema has none; only part `nav` is analysed).

## Tests

```
python -m pytest tests/unit/bag_analysis -q -o addopts=""        # 91 tests, no Docker (needs jsonschema; skipped without it)
docker run -d --name ba-test -p 127.0.0.1:55455:5432 -e POSTGRES_PASSWORD=t -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
BAG_ANALYSIS_TEST_DSN="host=127.0.0.1 port=55455 dbname=mission user=postgres password=t" \
  python -m pytest tests/integration/test_bag_analysis_postgres.py -q -o addopts=""   # migration up/down/up, store SQL, concurrent claim
docker rm -f ba-test
```
