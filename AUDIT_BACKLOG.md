# cloud_server — Audit Backlog

Findings from the full `cloud_server` + `../sati-client` audit of 2026-09-18
(inconsistencies, redundancies, improvement/optimisation opportunities,
principledness). Same conventions as `../sati-client/AUDIT_BACKLOG.md`: each item
has a severity and concrete pointers; ✅ DONE items were fixed, tested and committed
in this pass, the rest are verified against the code but deliberately deferred.
Client-side findings from the same audit are in that file's section **AA**.
`docs/BACKLOG.md` remains the place for incident write-ups; this file is the audit
punch list.

> Status: unit suite **555 passed / 0 failed** (was 537 passed / **18 failed** at the
> start of the pass). Integration/e2e suites were not run (they need the Docker
> stack). Line numbers are as of the commits named in each item.

---

## A. Fixed in this pass

### A1. ✅ DONE — force-cancel flag could stick at `True` forever — **high**
`RobotSpecV1.needs_order_cancel` (the operator "zombie order" escape hatch) was
edge-triggered against the previous robot object. If the flag was already `True`
the first time the dispatcher saw the robot — it was down or restarting when the
operator asked, i.e. exactly when the hatch is needed — the creation branch of
`Robot._on_robot_change` never looked at it, every later echo compared True to
True, and it neither fired nor cleared; re-POSTing just wrote True again. Now
level-triggered in `Robot._handle_force_cancel()`, called from both branches; the
clear is persisted *before* sending, and the explicit-cancel path's
one-cancel-at-a-time rule (`_has_outstanding_cancel()`) stops a stale still-True
echo from minting a second `cancelOrder`. The old test
`test_needs_order_cancel_true_to_true_does_not_refire` manufactured a state the
code never produces; replaced with stale-echo, persisted-spec and
first-creation tests (`tests/unit/test_force_cancel_order.py`).

### A2. ✅ DONE — timeout path double-sent `cancelOrder` — **medium**
`_wait_mission_timeout` sent a cancel unconditionally; on the `needs_canceled`
route one is normally already outstanding (and `handle_instant_action()` keeps
resending it regardless of mission), so the comment's "nothing will resend/track
this otherwise" was wrong. Gated on `_has_outstanding_cancel()`; covered by
`test_timeout_does_not_duplicate_an_outstanding_cancel_order`.

### A3. ✅ DONE — `cancelOrder` construction triplicated — **low**
Explicit cancel, force cancel and timeout cancel each built and tracked the action
by hand. One `Robot._send_cancel_order(action_id)`.

### A4. ✅ DONE — `self._database.create(...)` does not exist — **high**
`packages/controllers/mission/server.py` (detection results): `PostgresDatabase`
only has `async create_object(obj, publisher_id)`. The first FINISHED
`GET_OBJECTS` action raised `AttributeError`, swallowed by `run()`'s broad except,
skipping the rest of that state message (instant actions + mission update); later
messages then 404'd on `update_status`. Now `await ...create_object(..., uuid4())`.

### A5. ✅ DONE — `GET /api/v1/robots/{name}/status` returned the whole robot — **high**
sati-client's "clear fault" (`overrideRobotState`) fetched it as the *status* and
PUT it back as `{"status": <whole robot>}`; `RobotStatusV1` ignored the unknown
keys, so pose / online / battery / factsheet.custom_actions reset to defaults
until the robot's next state message. MSW mocks returned a status, so client tests
passed. Now returns `robot.status.dict()` (like the mission equivalent);
`docs/API_REFERENCE.md` and the e2e expectation updated; the client additionally
reads `fetchRobot().status` so it is correct against an older server too.

### A6. ✅ DONE — 22 handlers flattened every error to one status code — **medium**
`packages/database/postgres.py` already raises correct `HTTPException`s (404/400),
but `packages/api/main.py` handlers caught bare `Exception` and forced one code:
DB outage → 404, not-found → 400 (`update_robot`, `cancel_mission`,
`force_cancel_robot_order`, …), and `get_image`'s own 404 → 500. Added
`except HTTPException: raise` ahead of each. **Deferred remainder:** the fallback
code for a genuinely unexpected error is still 404/400 in those handlers rather
than 500 — changing it is a client-visible contract change.

### A7. ✅ DONE — undefined `logger` in robot registration — **high**
`main.py` `POST /api/v1/robots` error path called `logger.exception` with no
`logger` defined → `NameError` inside the except block → generic 500, real error
lost. This route runs on every robot startup.

### A8. ✅ DONE — orchestration proxy rejected PATCH — **medium**
`packages/api/orchestrator_proxy.py` allowed GET/POST/PUT/DELETE; the client's
per-service autostart toggle uses PATCH → 405.

### A9. ✅ DONE — graph-builder never awaited `_ensure_robot_exists` — **high**
Called un-awaited from the sync, worker-thread `_process_topology`; the coroutine
object is always truthy, so robot auto-registration never ran and its error branch
was unreachable ("coroutine was never awaited" at runtime). Now awaited in
`_handle_node_update` before `asyncio.to_thread`. **Behaviour note:** this turns
on a code path that has never executed in production — watch the first deploy.

### A10. ✅ DONE — mission-planner `/health` always unhealthy — **high**
`is_healthy(timeout=…)` / `is_running(timeout=…)` — neither accepts `timeout`; the
`TypeError` was swallowed by `DependencyHealthChecker`, reporting both critical
dependencies down forever. (`tests/integration/test_mission_database_postgres.py`
still calls `is_running(timeout=2)` — stale, see C3.)

### A11. ✅ DONE — graph-builder `/process_node` always "succeeded" — **medium**
It truth-tested a result *dict* and did blocking Arango/MinIO I/O on the event
loop. Now `await asyncio.to_thread(...)` and checks `result["success"]`.

### A12. ✅ DONE — 18 stale unit tests — **medium**
`tests/unit/test_rosbag_db_server.py` (17) and one `test_map_api.py` test predated
commit `ac723a3`, which deliberately moved rosbags to a flat robot-scoped bucket
with `.json` sidecars and made `load_map` preserve a stored datum. Tests rewritten
against the live signatures; no code regressed. The same stale calls remain in
`tests/integration/test_rosbag_db_integration.py` (C3).

### A13. ✅ DONE — ops scripts referenced services that don't exist — **medium**
`scripts/check_health.sh` probed graph-db:6001, image-db:6002, similarity:8003
(no such services) and skipped livekit:8006 / agent-orchestrator:8007;
`restart_services.sh` never rebuilt `agent-orchestrator-service`.

### A14. ✅ DONE — redundant function-local `import uuid` — **low**
~17 copies across `controllers/mission/server.py` and `api/main.py`. In
`_on_robot_change` it also made `uuid` function-local, a latent
`UnboundLocalError` for any earlier use. (`packages/api/server.py` still has ~12
inline `import uuid as _uuid` / `base64` / `math` — same cleanup, not done.)

---

## B. Security — needs an owner decision, not a code-only fix

### B1. Live credentials are tracked in git — **critical**
`docker_compose/.env` is tracked despite `.gitignore` listing `.env`, with
non-placeholder LiveKit key/secret and Postgres/Arango/MinIO passwords; the same
LiveKit key+secret+cloud URL are in `.env.example` and hardcoded as `os.getenv`
defaults in `packages/services/livekit/main.py:68-71` (whose comment points at a
"BACKLOG.md A3" that `ac723a3` deleted). **Action:** rotate the LiveKit key first,
then `git rm --cached docker_compose/.env`, ship a placeholder
`docker_compose/.env.example`, drop the source defaults and fail fast via
`packages/config.py` like the other secrets. Not done here: rotation and the deploy
env change are operator actions.

### B2. No authentication + an open SSRF relay — **high**
No auth dependency, API key or CORS policy anywhere in `packages/api`; the service
binds `0.0.0.0` on host networking. Anyone who can reach :8000 can delete
maps/robots/missions, force `cancelOrder`, mint LiveKit tokens with arbitrary
grants (`/api/createToken`) and get presigned MinIO upload URLs. Combined with
`orchestrator_proxy.py`: `POST /api/v1/robots` lets a caller set
`ip_address`/`entrypoint_port`, then `/api/v1/orchestration/{robot}/{path}`
forwards arbitrary methods, headers and bodies there — a relay into localhost
(Arango :8529, MinIO :9000) and the tailnet. `/stats` discloses internal URLs.
**Direction:** API-key/JWT dependency with separate robot vs operator scopes;
validate the proxy target against the robot subnet; strip hop-by-hop and
`Authorization` headers.

### B3. Default-credential fallbacks — **medium**
Compose reads `ARANGO_ROOT_PASSWORD` / `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`
with `openSesame` / `minioadmin` fallbacks, while `.env.example` and CLAUDE.md
document `ARANGO_PASSWORD` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`, which compose
never reads — following the docs yields a stack silently on default creds. Same
literals as dead fallbacks in `graph_builder/server.py`, `mission_planner/server.py`,
`topomap_dbs/client.py`, `api/server.py:547`. Use `${VAR:?}` and one name per secret.

---

## C. Principledness / consistency — deferred

### C1. Unit tests run on Pydantic 2.12; production pins 1.9.0 — **high**
`tests/requirements-test.txt` pins `pydantic>=2.0.0,<3.0.0`; every service
`requirements.txt` pins `==1.9.0` and CLAUDE.md forbids v2 idioms. The green unit
suite therefore validates a different runtime from what ships (v1-style `class
Config` only warns under v2). Align the test env to 1.9.0 (or migrate for real).

### C2. `packages/config.py` is not the single source of truth it claims to be — **medium**
Violators (grep-verified): `api/main.py:261-263` (`ws://localhost:8004`, direct
`os.getenv` for `GRAPH_BUILDER_WS_URL` / `MISSION_DISPATCHER_WS_URL` /
`MQTT_ENABLED`); `api/server.py:498-502`; `services/livekit/main.py` (port 8006,
ttl 36000) and `client.py:26`; `mission_planner/client.py` (`timeout=5`, URL
literal); ctor defaults in every `topomap_dbs/*/server.py`, `graph_builder/server.py`,
`controllers/mission/main.py`, `utils/mqtt_client.py`. Mission-controller magic
numbers (`MAX_INSTANT_ACTION_RESENDS=20`, `MAX_ORDER_MISMATCHES=40`,
`MAX_FINISHED_MISSIONS_TRACKED=256`, notify retries) are *counts per state
message*, so their real duration depends on the robot's state rate. Conversely,
compose sets env vars nothing reads (`DISTANCE_THRESHOLD=5.0` in compose vs the
3.0 graph-builder actually runs with; `KNN_K`, `RANGE_SEARCH_RADIUS`,
`MISSION_PLANNER_URL`, `LIVEKIT_URL`, …). MinIO console default :9001 collides
with mosquitto's websocket default :9001 under host networking.

### C3. Stale docs and tests — **medium**
- **`CLAUDE.md`** (not edited in this pass — it is the instruction file, flagged
  for the owner): says `packages/api/server.py` is the main FastAPI app (all ~60
  routes live in `packages/api/main.py`; `server.py` holds `ApiDelegationService`
  + WS managers); lists `packages/utils/base_client.py` (does not exist); the port
  table lists similarity:8003, graph-db:6001, image-db:6002, mission-dispatch:5000
  — none exist as HTTP services (the API talks to Arango/MinIO directly through
  `TopomapDatabaseClient`) — and omits agent-orchestrator:8007; says clients use
  bare `requests` (they use `httpx.AsyncClient`); says copy `.env.example` to the
  repo root (compose reads `docker_compose/.env`).
- **`docs/API_REFERENCE.md`**: documents a nonexistent `POST /api/v1/explore`; has
  nothing on `/maps*`, `/rosbags*` (8), `/base_models*` (5), `/createToken`,
  `/robots/{name}/map|cancel-order|actions|diagnostics`, `nav2_bt_*`,
  `/missions/{name}/plan`, `/navigate/waypoints`, the orchestration proxy; says
  mission `/status` is an alias for the full mission (it returns the status only);
  its `/ws/mission` example shows `progress`/`node_status` fields the payload lacks.
- **Stale integration tests:** `tests/integration/test_rosbag_db_integration.py`
  (old map-scoped signatures), `test_mission_database_postgres.py`
  (`is_running(timeout=2)`).
- `.coverage` and `coverage.xml` are tracked in git and rewritten by every test run.

### C4. Mission WebSocket payload omits fields the client needs — **medium**
`mission_update` (broadcast `api/server.py:1893-1917` and connect snapshot
`api/main.py:1599-1626`) omits `task_status`, `node_status`, `held`,
`held_reason`, so the client's "N/M waypoints" and held banner refresh only on its
10 s REST poll. Build both payloads from one shared function. Likewise
`robot_update` sends `pose` without `map_id`, which the client shallow-merges, so
`pose.map_id` flips between the REST value and `undefined` every poll.

### C5. `task_status` is never filled for planner-created missions — **medium**
It records a waypoint only when `allowedDeviationXY == 0`, but
`mission_planner/server.py:375` emits `0.2` and the `Pose2D` default is `0.1`, so
every `/api/v1/navigate` mission falls back to the client's pose-proximity
heuristic. Replace the `== 0` sentinel with an explicit checkpoint flag.

### C6. Whole-spec read-modify-write loses updates — **medium**
The force-cancel clear, `_process_datum_message`, and every API get→set→
`update_spec` write the full spec JSON. A datum message landing between the API's
`needs_order_cancel=True` write and its echo writes the old spec back and silently
drops the request after the API returned success; concurrent map/teleop changes
can be clobbered the same way. Add a field-level `jsonb_set` update for one-shot
flags, or move commands off the spec onto a command channel.

### C7. What should force-cancel do to a *tracked* mission? — **medium, design**
A FINISHED `cancelOrder` is read as node CANCELED; with `needs_canceled` False the
dispatcher then re-sends the node's order — so a force-cancel while a mission is
tracked is undone within one state message. Decide: fail/cancel the tracked
mission, or scope the CANCELED reading to cancels whose `actionId` belongs to it.

### C8. `_wait_mission_timeout` can strand a deleted mission as current — **medium**
On `PENDING_DELETE` it deletes the row and returns without `get_next_mission()` /
IDLE / cancel; the queue stays blocked until `MAX_ORDER_MISMATCHES` rescues it.

### C9. `get_mission_errors()` trusts any FATAL error — **medium** (from `docs/BACKLOG.md`)
Still open: a lingering FATAL from an unrelated order can fail a fresh mission.

### C10. Unvalidated spec writes — **low/medium**
`update_robot` / `update_mission` accept a raw `dict` and `setattr` arbitrary spec
fields with no `validate_assignment`. `UpdateRobotMapRequest.map_id` is required,
so the client's `assignRobotMap(name, null)` can only ever 422.

---

## D. Performance / reliability — deferred

### D1. Blocking I/O on event loops — **medium**
Mission controller: `requests.get/post` to `mission_ctrl_url` with **no timeout**
(a hung mission-control freezes dispatch for every robot), sync retries in
`_process_notify_node`, `time.sleep` in async `postgres._get_connection`. API: sync
MinIO/Arango calls inside async handlers (`get_image`, `list_node_images`,
`store_image`, all rosbag/model methods) and a sync `health_checker.check_all()`
in `async /health`. Planner: sync python-arango in `find_closest_node_*`,
`find_path`, `get_node_poses` (one `get_node` per waypoint — batch with one AQL
`FILTER node._key IN @keys`).

### D2. Fire-and-forget status writes — **medium**
~10 `asyncio.ensure_future(update_status(...))` with no error handling; failures
surface only as "Task exception was never retrieved", and ordering across pool
connections is not guaranteed (the `_finished_missions` comments already work
around this). One `_persist_status()` with a done-callback logger, ideally a
per-object serial write queue.

### D3. Path planning minimises hops, not distance — **medium**
`graph_db/server.py` `SHORTEST_PATH` has no `weightAttribute`, and the edge weight
key is inconsistent (`distance` from graph-builder, `weight` from API `load_map`).

### D4. Spatial queries are full collection scans — **medium**
`_knn_arango` / `_range_arango` compute `SQRT` over every node and sort the
collection, on every incoming node update. `SpatialIndexManager` (294 lines) builds
a *geo* index on Cartesian metres in an unused collection and no query uses it —
replace with a persistent index + bounding-box prefilter and delete the manager.

### D5. graph-builder memory/lifecycle — **medium**
The cleanup loop has no try/except and iterates dicts that the paho thread and
worker threads mutate (one "dictionary changed size" kills it permanently, after
which both maps grow unbounded); buffered base64 images for nodes that never
arrive live up to 3600 s although `image_buffer_timeout` is 30 s;
`_mqtt_connected` is never set True so `is_healthy()` is always False; late images
take `map_id` from the MQTT payload while nodes use `robot.current_map`, so an
image can land in a different bucket than its node.

### D6. Postgres watcher hygiene — **low/medium**
`self._connection` replaced without closing (one leak per 60 s idle timeout);
a silent broad `except` — the file's own comment says that is what hid the earlier
busy-loop bug; every object logged at WARNING on each resync. WebSocket handlers
in `api/main.py` catch only `WebSocketDisconnect`, leaking the `ws_manager` entry
on any other receive error.

---

## E. Redundancy / dead code — deferred

- **Duplication:** `main.py` argparse+uvicorn blocks copy-pasted across 5 services
  (→ a `run_service()` helper in `utils/service_utils.py`); `UpdatePublisher` ≡
  `InsightPublisher`; `MissionPlannerClient` / `LiveKitClient` httpx boilerplate;
  two divergent node-ingest paths in graph-builder (`_process_topology` vs
  `process_node_update`); `update_robot` ≡ `update_mission`; the factsheet-request
  construction twice in `_on_robot_change`.
- **Dead:** `packages/api/client.py` (empty shell); `_notify_graph_builder`
  (builds a dict, only logs); `plan_mission` / `_call_mission_planner` (no callers,
  drops `map_id`); the mission/robot branches of `_connect_to_backend`
  (`MISSION_DISPATCHER_WS_URL=ws://localhost:5000` targets nothing);
  `GraphDatabaseService.update_node` / `delete_node` / `remove_node` / `find_path`
  (the latter fakes "10 m per hop"; `delete_node` would leave dangling edges);
  `MQTT_RECONNECT_PERIOD`, `DATABASE_RECONNECT_PERIOD`,
  `RobotServer._mqtt_on_connect`, unused `sys`/`cast` imports and a double
  `_detection_results_object` init in the mission controller;
  `scripts/update_robot_custom_actions.py` targets a nonexistent :5001.
- **API surface with no client caller:** `GET /maps/{id}`, `PUT /maps/{id}/datum`,
  `POST /navigate/waypoints`, `GET /rosbags`, `GET /base_models/{id}`,
  `/detection_results*`, `/stats`.
- Third-party images pinned to `:latest` (mosquitto, arangodb, minio).
