# Scene graph v2: state at end of session and next steps (LACI side)

Branch `feat/scene-graph-v2` (CIMBI's Morse engine with chart_radius, the lazy planner `09a4e99` behind
`SCENE_GRAPH_PLANNER_MODE=lazy` (default `full_load` when written; production lazy since 2026-10-13 19:00 UTC; `full_load` existed until its removal on 2026-10-13, lazy is the only mode), the store read-path patch `16aee6a`; `main` merged as `39fd715`, then `7710d7a`:
single alembic head, pinned test image with scipy; HEAD `7710d7a`, origin was @485f39d before those two). Targeted
tests 339 passed / 3 skipped (MinIO-only) on 2026-10-13 before the main merge (not re-run since); `tools/scene_graph_e2e/run.sh` 32 passed in
`full_load` and `lazy` (while both existed); full pinned suite 4158 passed / 0 failed after `7710d7a` (2026-10-13), per CIMBI (not re-run by us).
Findings, decisions and open items of the planner-upgrade work: `planner_findings_2026-10-13.md`.
Client: `sati-client` commit 7178d1a (tiling, abort/pause, epoch/ETag, stale view) is in; the
client repo also has unrelated uncommitted edits (DeckGLMap, relief, LiveKit): not ours.

## Where things are documented
- `runbook.md`: operations, settings table, counters, rebuild/backfill, known limitations.
- `integration.md`: worker/planner/read-API contract, rebuild order and memory bound,
  "Format bump (FORMAT 4, pipeline `morse/2`)" section.
- `formulation.md`, `audit_ledger.md`, `handoff_laci_morse.md`: CIMBI's (engine, ledger).
- `planner_upgrade.md`: tracked (merged with the lazy planner); section 13 is partly obsolete
  (repeated leaf ids no longer fall back; marked there). `planner_findings_2026-10-13.md`: this session's findings (UNTRACKED).

## Env settings added in this effort (all in config.py, compose, `.env.example`)
Planner: SCENE_GRAPH_PLANNER_ENABLED (default off), SCENE_GRAPH_WIDTH_CHECK_ENABLED (off),
TOTAL_BUDGET_S 3.5, PLAN_MAX_INFLIGHT 4, CACHE_VERSIONS_PER_MAP 2, WIDTH_MARGIN_M 0.05.
Worker: WORKER_THREADS, MAX_RESIDENT_ENGINES 32, EVICT_IDLE_S 60, LAZY_LOAD, PREFETCH_*,
MEMORY_RETRY_LIMIT, RECEIPT_WINDOW_S 300, SKIP_FOLDED true, DELETE_LOCK_* .

## Remaining steps (new session)
1. Merge `feat/scene-graph-v2` to `main` after final checks. OUTWARD-FACING: needs the
   user's go. Remote has origin/feat/scene-graph-v2 and -morse (both pushed by CIMBI).
   Stray local branches `worktree-agent-*` and `feat/scene-graph-morse-laci-tests` may be
   deleted once merged (check each is merged first).
2. Database generation for the real maps with the Morse engine (store FORMAT 4, pipeline
   morse/2; existing stores get a new generation, none in production). CIMBI waits on the
   user's go.
3. Production checks (need DB access, read-only):
   `SELECT name, status->'factsheet'->>'length', status->'factsheet'->>'width',
   status->'factsheet'->>'height' FROM robotobjectv1 WHERE name IN
   ('formidable-peacock','masked-frigatebird')`. Without factsheet rows the worker uses the
   default footprint (`default_r_d` counter, warning once per robot).
   Migration `20261012_01_blocked_geometry` (nullable `blocked_graph_nodes` columns) is NOT applied in the live DB,
   which is at `20261013_01` (single head since `7710d7a`); it applies at API start (`alembic upgrade head`), see
   `runbook.md` rollout step 2. Still to run, along with `tools/scene_costmap_stored_backfill.py` (Arango
   `costmap_stored_ms` on older topomap nodes; dry run first, `--apply`), before switching the worker on.
4. Check the live API for stray test maps/missions left by the earlier mistaken e2e run
   (batch F ran tests against localhost:8000 once).
5. Rollout: enable `SCENE_GRAPH_PLANNER_ENABLED` on ONE map, width check off, compare against
   the old planner; then run the width gate once with a real factsheet (rehearsal F-7: 0.1 m
   widths in office03/04/08; CIMBI's route-width fix 029e117 should cover it, re-verify).
   Validate the 30 deg turn threshold against the real controller before enabling
   SCENE_GRAPH_WIDTH_CHECK_ENABLED.
6. Keep Hospital / simulation / open maps `scene_graph_enabled=false` until the open-field
   decision (Morse reports Hospital 0.73 s, so this can be revisited).
7. `place_mult` and `sight_range` are removed from `SceneParams` (`api.py`), the store, the params hash and
   the host (FORMAT 4); nothing writes them any more. integration.md "Format bump" lists what is left (drop the
   always-zero `dependent` plane from the served raster).

8. Planner upgrade follow-ups (decisions in `planner_findings_2026-10-13.md` section 7): Arango read-model mirror
   (start or not), tile-cache-by-hash (store side done in `16aee6a`) and bounded stage 1. The canonical integer
   tie-break ask was SENT and AGREED by CIMBI (rule: findings section 10): the lazy vs `store.load` equality
   test is `tests/unit/test_lazy_plan_equality.py` and `lazy_plan.py` follows the same key rule (`b997c66`); the engine
   side landed as `88e5651`.

## Known limitations / open decisions
- Client: hide links narrower than the robot (slivers stay in `query().links`);
  `chart_keys_truncated` not surfaced; minio-py pool size not raised.
- A genuinely narrower second door between the same two charts is not reported when a wider
  one exists (CIMBI, documented in formulation.md).
- Constant-width loops and long corridors are ONE region; a room is its own region only when
  its peak clearance exceeds the exit saddle by more than tau0 (about 1.8 m for 1.2 m
  corridors).
- Rebuild past `rebuild_max_age_s` commits what is folded and ingests the rest incrementally.
- 422 for a too-high level is checked after the query (forged matching ETag gives 304).
- Mixed fleets: the evidence bound assumes r == r_d; CIMBI must re-derive before a mixed fleet.
- Deferred by the user: r_d rule for long robots; authz.
- `begun_ms >= request.at` resume rule: unsafe only if graph-builder clock runs ahead of
  the API's; keep NTP in the ops checklist.

## How to run checks
```
S=/tmp/claude-1000/-home-satiadmin-satinavrobotics-web-stack/8605a980-67d0-4e3a-a9e6-f199edeb4610/scratchpad/v1
PYTHONPATH=$(echo $S/lib/python3*/site-packages):. ARANGO_PASSWORD=x MINIO_ACCESS_KEY=x \
  MINIO_SECRET_KEY=x POSTGRES_PASSWORD=x $S/bin/python -m pytest -p no:randomly tests/unit -q
SG_E2E_PYTHON=$S/bin/python PYTHONPATH=... tools/scene_graph_e2e/run.sh   # no extra args
```
The scratchpad venv may be gone in a new session: rebuild from tests/requirements-test.txt
(pydantic v1, livekit-api, alembic). Never run tests against the live stack on :8000.

## Working rules
LACI owns integration (worker, read API, client, planner, lifecycle, events, ops); CIMBI owns
`packages/scene_graph`, `api.py`, `engine.py`, `store.py`. Commit only your own files; never
`git add -A`. Do not message CIMBI without the user's go-ahead.
