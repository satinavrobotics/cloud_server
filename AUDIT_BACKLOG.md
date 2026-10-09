# cloud_server — Audit Backlog

Findings from the full `cloud_server` + `../sati-client` audit of 2026-09-18
(inconsistencies, redundancies, improvement/optimisation opportunities,
principledness). Same conventions as `../sati-client/docs/AUDIT_BACKLOG.md`: each item
has a severity and concrete pointers; ✅ DONE items were fixed, tested and committed
in this pass, the rest are verified against the code but deliberately deferred.
Client-side findings from the same audit are in that file's section **AA**.
`docs/BACKLOG.md` remains the place for incident write-ups; this file is the audit
punch list.
Open refactors and gaps from the SLAM-toggle / relocalization work (2026-10-04) are
listed in `docs/RELOC_FOLLOWUPS.md`.

> Status: unit suite **555 passed / 0 failed** (was 537 passed / **18 failed** at the
> start of the pass). Integration/e2e suites were not run (they need the Docker
> stack). Line numbers are as of the commits named in each item.
>
> Session log (2026-09-18), oldest first: `68f41af` mission-dispatch · `2201e0b` API ·
> `c5562fd` services · `e387736` stale tests · `17d7f5b` ops scripts · `b62dcc5` this
> file · `f4a44b4` `CLAUDE.md`. Nothing pushed. The client half, its commits, and the
> one item left open (UI bugs after starting a mission, which the client's mock
> backend cannot reproduce) are in `../sati-client/docs/AUDIT_BACKLOG.md` AA14 and its
> session summary.

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
`_wait_mission_timeout` (now `_fail_mission_on_timeout`) sent a cancel unconditionally; on the `needs_canceled`
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

### A15. ✅ DONE (`6da917c`) — VDA5050 order/node ids repeated across runs and revisions — **high**
`orderId` was `{mission}-n{idx}` with `orderUpdateId` always 0, so a mission
re-created under a name that was used before (the 2026-09-15 incident: delete +
re-create) sent the *same* ids as the earlier run — which a robot that ignores
already-seen ids drops, and which also let the previous run's leftover `lastNodeId`
read as progress here. The same id was also reused within a run when a cancelled node
was resent with new content (operator route update / edge-blocked reroute). Ids are now
`{mission}-r{run_id}[v{order_rev}]-n{idx}[-s{seq}]`: `MissionStatusV1.run_id` is
assigned once and persisted *before* the first order, `order_rev` is bumped and
persisted before a cancel-and-resend; both are dispatcher-owned (the API ignores them
on create, preserves them on a status write). A mission already running before this
keeps its legacy ids, so no deploy ordering is needed. All id building/matching/parsing
is in `packages/controllers/mission/order_ids.py` (`docs/deployment_ontology.md`
describes the scheme). Tests: `tests/unit/test_mission_order_ids.py`.
Left for others: (1) the robot client must record only *completed* orders in a
replay guard, or our legitimate retries (mismatch resend, restart resume) are
dropped; (2) `sati-client`'s `MissionStatus` type does not list `run_id` /
`order_rev` (harmless — extra JSON is ignored); (3) the Docker e2e test
`test_state_updates_mission_progress` was adapted but not run.

### A16. ✅ DONE (uncommitted) — a cancel released a teleoperated robot in the dispatcher only — **high**
The dispatcher sends `stopTeleop` only while it believes the robot is `TELEOP` and
`switch_teleop` is false. But `update_robot_state()` treated *any* acknowledged instant
action other than `startTeleop` (a finished `cancelOrder`, `factsheetRequest`, …) as
"stop teleop", and every mission-end path reset the robot to `IDLE`, so after a cancel
the dispatcher believed the robot was free and an operator's stop sent nothing. That
was invisible while a robot's `cancelOrder` also ended its pause; once a robot stays
`paused` after a cancel (robot change R1) it would have been stuck until someone
started and stopped teleop again. Now only `startTeleop`/`stopTeleop` acks move the
state, and mission end (`_set_robot_idle_after_mission`) and mission start leave
`TELEOP` alone. Tests: `tests/unit/test_mission_teleop_state.py`.
Left open: (1) for a robot whose cancel *does* end its pause, the dispatcher now stays
`TELEOP` until a `stopTeleop` is acknowledged (the operator's stop, or any robot-object
update while `switch_teleop` is false), where it used to fall back to `IDLE`; using the
robot's reported `paused` to clear it would remove that dependency (`paused` is not read
anywhere today); (2) a `pause_order` action sets `TELEOP` without touching
`switch_teleop`, so the next robot-object event sends `stopTeleop` at once — whether
that is intended (auto-release) was not checked; (3) not run against a simulator.

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

### C1. Unit tests run on Pydantic 2.12; production pins 1.9.0 — **high** (mitigated 2026-10-09, mission dispatch audit)
**Mitigated:** `scripts/run_unit_tests_pinned.sh` runs the suite in `tests/Dockerfile.unit` (Python 3.10 +
the mission and API service pins, `httpx<0.28` for the old starlette `TestClient`). The host env stays on
pydantic 2 (host Python is 3.12, where 1.9.0 does not install), so `tests/requirements-test.txt` is
unchanged. Whole `tests/unit` on the pins: all green (`-m unit`: 3061 passed, 84 unmarked tests deselected).
Original finding:
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
- ✅ DONE — **`CLAUDE.md`** rewritten to match the code (on the owner's request):
  `packages/api/main.py` holds the app + all routes and `server.py` the
  `ApiDelegationService`; the port table now lists the services that exist
  (no graph-db:6001 / image-db:6002 / similarity:8003; mission-dispatch has no HTTP
  port; agent-orchestrator:8007 added); `topomap_dbs/*` are in-process libraries;
  PostgreSQL LISTEN/NOTIFY — not HTTP — is the API↔mission-dispatch interface; the
  nonexistent `utils/base_client.py` is gone; service clients are `httpx`; and the
  two env files (`docker_compose/.env` vs the process env `config.py` validates) and
  their different variable names are spelled out.
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

### C5. ✅ DONE — `task_status` is never filled for planner-created missions — **medium**
**Fixed** (`packages/controllers/mission/server.py` ~3318-3343, `649290e`/audit rounds): every waypoint now counts whatever its allowed deviation (`task_status[node] = idx` on each reached waypoint); the `== 0` sentinel is gone. Covered by `tests/unit/test_mission_lifecycle_fixes.py` (progress for non-zero deviation). Original finding:
It records a waypoint only when `allowedDeviationXY == 0`, but
`mission_planner/server.py:375` emits `0.2` and the `Pose2D` default is `0.1`, so
every `/api/v1/navigate` mission falls back to the client's pose-proximity
heuristic. Replace the `== 0` sentinel with an explicit checkpoint flag.

### C6. Whole-spec read-modify-write loses updates — **medium** (mostly fixed: mission dispatch audit 2026-10-09)
*Status:* the dispatcher's force-cancel clear and `_process_datum_message` now write one field
(`update_spec_fields`), and the API's mission cancel and PUT write only the fields they change (W12), so
neither can revert a dispatcher replan patch. Still whole-spec: the API's robot `PUT`/registration and
`POST .../cancel-order` (`update_spec`), so a datum landing between the API's read and write can still be
clobbered by it; move those to `update_spec_fields` too. Original finding:
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

### C8. `_wait_mission_timeout` can strand a deleted mission as current — **medium** (fixed in audit round 3)
*Status:* fixed in audit round 3 (X3: the timeout runs on the robot's message loop and a deleted mission
releases the queue). Original finding:
On `PENDING_DELETE` it deletes the row and returns without `get_next_mission()` /
IDLE / cancel; the queue stays blocked until `MAX_ORDER_MISMATCHES` rescues it.

### C9. ✅ DONE — `get_mission_errors()` trusts any FATAL error — **medium** (from `docs/BACKLOG.md`)
**Fixed** (`2be7831`, mission dispatch audit rounds 1-3): `_is_foreign_error` (`controllers/mission/server.py` ~3776) ignores a FATAL whose generated ids all belong to another run, revision or mission (logged once per `_foreign_error_refs` key), and `_track_stale_fatal` ignores unreferenced FATALs that predate the order. Tests: `tests/unit/test_mission_order_rejection.py::test_fatal_with_foreign_node_does_not_fail_current_mission`. Original finding:
A lingering FATAL from an unrelated order could fail a fresh mission.
A FATAL `robotBaseNotReadyError` (no references) now gets a `failure_reason`; the lingering-FATAL attribution issue remains.

### C10. Unvalidated spec writes — **low/medium**
`update_robot` / `update_mission` accept a raw `dict` and `setattr` arbitrary spec
fields with no `validate_assignment`. `UpdateRobotMapRequest.map_id` is required,
so the client's `assignRobotMap(name, null)` can only ever 422.
*Status (2026-10-09):* the second half is moot: `UpdateRobotMapRequest` is gone and `PUT /robots/{r}/map` answers 410 (maps U6, `tests/unit/test_maps_u6.py`). The first half (raw dict + `setattr`, no field allowlist) is being fixed in the 2026-10-09 round 2; see I below.

### C11. FATAL-error references parsed as node indexes, action ids included — **medium** (mostly fixed)
*Status (2026-10-09, verified in code only):* `get_mission_errors` now maps a nodePolicy `actionId` to its node (`order_ids.node_of_reference`/`node_index`) and skips references of another order (`order_ids.is_reference_of(...) is False`). Not verified: a `…-instantaction-n{headerId}` id that `order_ids` cannot attribute to an order (`is_reference_of` returns None) still goes through `node_index`; no test covers a failed instant action. Original finding:
`get_mission_errors()` (`controllers/mission/server.py`, see also C9) parses
`rsplit("-n")[-1].rsplit("-s")[0]` for `referenceKey in node_id/nodeId/action_id/
actionId`, which reads the suffix of an *action* id as a `mission_tree` index. Instant-action
ids are `…-instantaction-n{headerId}`, so a failed instant action is attributed to
`mission_tree[headerId]` whenever that index exists. Pre-existing; found while doing A15.
Fix: only parse `nodeId` references (`order_ids.node_index`), and match `actionId`
references against the tracked actions instead.

### C12. Instant-action ids are inconsistently scoped — **low/medium** (restart repeat fixed: mission dispatch audit 2026-10-09)
Outgoing `headerId`s (and so the `instantaction-n{headerId}` ids) are now seeded from the clock
(`HEADER_ID_EPOCH`/`HEADER_ID_RATE`, W13) and no longer restart at 0 after a dispatcher restart; the
ids still carry no mission/run token. Original finding:
The mission cancel / timeout-cancel ids carry the run prefix (A15), but the bare
`instantaction-n{headerId}` ids (`_on_robot_change` custom actions and the like) and
`force-cancel-instantaction-n{headerId}` carry no mission or run, and `_header_id`
restarts at 0 on every dispatcher start — so they repeat across restarts. Harmless
unless a robot dedupes on `actionId`; if it does (unverified), give them a per-process
token like the run id.

### C13. Order revision is bumped after the robot confirms the cancel — **low, design**
A15 bumps `order_rev` when the robot reports the node cancelled, but the route change
is applied when the API update arrives. Anything that sends an order inside that window
(none found in normal operation) would put new content under the old id. Whether a
dispatcher restart in the window resumes with the new route depends on whether the DB
copy already holds it (not checked). Bumping at update time instead moves the resend
onto the order-mismatch path (budget `MAX_ORDER_MISMATCHES` = 40 state messages) and
skips the explicit "canceled" branch — a flow change, not a tweak.

### C14. `bearing_deg` is documented one way and implemented another — **medium**
`utils/geo.py` and `RobotDatumV1` / `MapObjectV1.datum_bearing_deg` describe it as the
"angle from +X to true north", but `gps_to_local` computes `x = east·cos b + north·sin b`,
i.e. with b = 0 the local +X axis points *east* (the doc's wording would give 90°). The
code behaves as "local +X rotated b° counter-clockwise from true east". The datum is
also assumed *true*-north: nothing accounts for grid north / meridian convergence
(≈ −1.44° at the reporting site, ≈ 2.5 m lateral error per 100 m). The robot publishes
a constant 0.0 today. Decide the convention, fix the docstrings (or the maths), and pin
it with a round-trip test against a known bearing before any robot sends a non-zero
value. Note the planner converts GPS goals with the *map's* datum, not the robot's; the
robot's is only used to seed a map that has none (`_process_datum_message`).

**Update 2026-09-28:** convention decided and documented (angle of +X from east, CCW, as the
code always did) and pinned by tests. The grid-north part is solved by the datum `frame`:
the robot's VDA5050 client sends `"frame": "utm"` (+ zone, hemisphere, datum E/N), and
`geo.py` / the client's `mapTransform.ts` convert UTM-frame x/y with the exact UTM
projection, ENU-frame x/y (sim, orchestrator anchor) in the tangent plane. Left: maps whose
datum was auto-seeded before the frame existed are stored as `enu`; one from a real robot
needs `PUT /api/v1/maps/{id}/datum` with `datum_frame: "utm"`.

### C15. An unknown `operatingMode` drops the whole state message — **medium**
`VDA5050OperatingMode` accepts only AUTOMATIC / MANUAL / SEMIAUTOMATIC / SERVICE /
TEACHIN (and `""`). Any other value (e.g. a robot reporting `TELEOPERATION`) fails
`VDA5050State` validation, and `_on_mqtt_message` logs a warning and discards the *entire*
message — including the heartbeat, so the robot goes offline after `heartbeat_timeout`
and its mission stalls. Nothing in the dispatcher reads `operatingMode` (only the agent
orchestrator forwards it). Make the field tolerant (unknown → keep the message, log
once) rather than fatal, and agree the value a teleoperated robot reports.

### C16. `pause_order` detection reads only `actionStates[0]` — **low/medium**
`update_mission_node_state` (`controllers/mission/server.py`, `# TODO(Nico): fix the
action states index`) looks at the first action state only: a pause action that is not
first is missed, and an ACTION node whose `actionStates` is empty raises `IndexError`
(no guard). Instant-action states are appended to the same list, so "first" is only
right by convention. Match the state whose `actionId` belongs to the current node
(`…-n{idx}-s{seq}`), or scan all entries; `paused` is available too and unused.

### C17. Repeat, then-run, `wait` and multi-node completion are untested on a real robot — **medium**
`repeat`/`then_run`/`wait` and the per-node reading of `missionStatus: "completed"` were
built and unit-tested against the dispatcher only (`tests/unit/test_mission_repeat_chain_wait.py`).
Unverified against the robot client (`sati_vda5050_client/src/vda5050_client_node.cpp`):
(a) that a second order under a new `orderId` right after a completed one is accepted and
clears the old `missionStatus` before the next `/state` (the dispatcher ignores a
`completed` whose `lastNodeId` is not of the current run, for multi-node route/move nodes
only); (b) that a route that starts at the point the previous one ended is accepted (a
route split by a wait is `route, wait, route`, the second starting at the next waypoint);
(c) how a chained mission created by `then_run` orders against other queued missions (it is
queued last). Also open: an endless `then_run` cycle leaves one finished mission object
per lap in the database; nothing prunes them.

---

## D. Performance / reliability — deferred

### D1. Blocking I/O on event loops — **medium** (mission controller notify/charging hook fixed: W3)
*Status:* the mission controller's webhook calls (notify nodes, the charging hook) run in a worker thread with
a capped timeout and bounded retries (W3). Open: `time.sleep` in `postgres._get_connection`, the API and
planner items below. Original finding:
Mission controller: `requests.get/post` to `mission_ctrl_url` with **no timeout**
(a hung mission-control freezes dispatch for every robot), sync retries in
`_process_notify_node`, `time.sleep` in async `postgres._get_connection`. API: sync
MinIO/Arango calls inside async handlers (`get_image`, `list_node_images`,
`store_image`, all rosbag/model methods) and a sync `health_checker.check_all()`
in `async /health`. Planner: sync python-arango in `find_closest_node_*`,
`find_path`, `get_node_poses` (one `get_node` per waypoint — batch with one AQL
`FILTER node._key IN @keys`).

### D2. Fire-and-forget status writes — **medium** (mission controller fixed: W7)
*Status:* the mission controller's robot and mission status writes go through a per-row serialized queue
(`_queue_status_write`): retried, tracked, throttled and flushed at shutdown (W7, W9). Open only outside the
mission controller, if any `ensure_future(update_status(...))` is left. Original finding:
~10 `asyncio.ensure_future(update_status(...))` with no error handling; failures
surface only as "Task exception was never retrieved", and ordering across pool
connections is not guaranteed (the `_finished_missions` comments already work
around this). One `_persist_status()` with a done-callback logger, ideally a
per-object serial write queue.
`Robot._persist_current_mission_status()` (awaited, added in A15 for the writes that
must land *before* an order goes out) is a starting point for the shared helper.

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
image can land in a different bucket than its node. *(Map part fixed in maps M2: images and
nodes both go to the robot's open session; `robot.current_map` itself was removed in U6.)*

### D6. Postgres watcher hygiene — **low/medium** (watcher part fixed: W1)
*Status:* W1 made the watcher/handlers resilient (no silent broad `except`, the per-object resync warning
is gone). Open: the leaked `self._connection` per idle timeout (check) and the WebSocket handlers in
`api/main.py`. Original finding:
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
  `RobotServer._mqtt_on_connect`, `VDA5050Order.from_mission` (no callers; emits a
  bare-name `orderId` outside the A15 scheme), unused `sys`/`cast` imports and a double
  `_detection_results_object` init in the mission controller;
  `scripts/update_robot_custom_actions.py` targets a nonexistent :5001.
- **API surface with no client caller:** `GET /maps/{id}`, `PUT /maps/{id}/datum`,
  `POST /navigate/waypoints`, `GET /rosbags`, `GET /base_models/{id}`,
  `/detection_results*`, `/stats`.
- Third-party images pinned to `:latest` (mosquitto, arangodb, minio).

### E-fixed. Dead code removed — mission dispatch audit 2026-10-09
`MQTT_RECONNECT_PERIOD`, `DATABASE_RECONNECT_PERIOD`, `RobotServer._mqtt_on_connect`,
`VDA5050Order.from_mission`, the unused `sys`/`cast`/`os` imports and the double
`_detection_results_object` init in the mission controller are gone (the "Dead:" bullet above, mission-controller
part, is done). Kept: `push_telemetry`/`TelemetrySender` and the charging hook (legacy, still wired to CLI
flags), and `packages/controllers/mission/tests/{test_context,client}.py` (the e2e conftest imports them).

### H. Mission dispatch audit 2026-10-09 — fixed, and open follow-ups
Fixed: C12 (restart repeat, W13), `failure_category` now set by the dispatcher (TIMEOUT, ROBOT_APP, CANCELED),
factsheet `custom_actions` cleared by an empty list, edge ids carry the node index, stale tests
(run-change confirmation, watcher retry, recorder) brought in line, `_pre_drop_pose` aliasing. Open:
- (a) Robot status whole-row writes between the API and the dispatcher: a factsheet / `PUT status` change
  can be reverted by the dispatcher's next write. Proposed `update_status_fields` (jsonb merge) in `postgres.py`.
- (b) `connection` OFFLINE/CONNECTIONBROKEN does not mark the robot offline at once; the heartbeat timeout is
  the only source.
- (c) A mission that was started and then cancelled while its robot is offline blocks that robot's queue
  until the robot returns (workaround: operator force-cancel).
- (d) The headerId seed comes from the clock and is not persisted (a clock stepped back could repeat ids).
- (e) The mission-dispatch healthcheck (heartbeat file) is not acted on by plain `docker compose`; it needs an
  external watchdog (autoheal or a systemd timer).
- (f) `deadline` is not enforced: it is in `EDITABLE_SPEC_FIELDS`, in the API docs only as a field (`docs/API_REFERENCE.md`, "ISO 8601 timestamp", never as enforced;
  the mission type), and `MissionFailureCategoryV1.DEADLINE` exists, but nothing compares it with the clock.
  Enforcing it needs a timer like `_arm_mission_timeout` (also for PENDING missions: fail with
  `DEADLINE` once `now > deadline`, cancelling the robot order if started) and a decision on naive/aware time.
- (g) Time sources: stored mission timestamps are naive local `datetime.now()` in the dispatcher; correct only
  while the container's TZ is UTC. `docker_compose` sets no `TZ` for `mission-dispatch` (Postgres runs with
  `timezone=UTC`); the python base image defaults to UTC, but nothing enforces it.

### C18. No robot publishes `heightMax` — **low**
mission-dispatch now keeps VDA5050 `physicalParameters.heightMax` as `factsheet.height` (`da91987`,
-1 until sent) and the client raises the robot by it (2.5D map view), but the ROS client only
sends length and width, so every robot is drawn 0.35 m tall. Add `heightMax` to the factsheet the
robot publishes (and to the `factsheet_data` of its registration in `create_robot`).

## F. Asked for by the client redesign (2026-10-03) — deferred

Client side: `../sati-client/docs/AUDIT_BACKLOG.md` section **AB**.

### F1. No fleet-wide insights/events stream — **low**
The Workbench layout's Events tab can only show client-side sources (mission status changes,
mission failures, nav-supervisor transitions); agent-orchestrator insights are per robot
(`AgentModal`). A fleet-wide `/api/agent/…` listing or WebSocket would let Events and the
Utilities → Insights tile work without a selected robot.

### F2. Robot status has no timestamp — **low**
The Workbench Diagnostics tab lists faults and not-ready robots, but `robot.status` carries no
"since" time, so every row shows "now". Add the time the error / readiness state began.

## G. Map type conversion geo ↔ local (2026-10-03) — built, deployed with `6b62d6b` (2026-10-09)

Design: `docs/satinav-maps-redesign.md` §17. Client side: `../sati-client/docs/AUDIT_BACKLOG.md` **AB8**.
Branch `feat/map-type-convert`. Tests: `tests/unit/test_map_type_convert.py`,
`tests/integration/maps/run_type.sh` (throwaway Postgres, `checks_type.py`).

### G1. ✅ DONE — deploy steps
*Merged to `main` (`d2d1904`, `abbf946`) and part of the 2026-10-09 deploy (`6b62d6b`); the steps below are the original plan.*
No Alembic migration. Rebuild and restart from the same commit, in this order:
mission-dispatch, graph-builder-service, mission-planner-service, then api-delegation-service
(only the new API can create a rotated geo map; old consumers ignore `geo.bearing_deg`). Then
the client. Rollback: convert any rotated geo map back (local, or geo with bearing 0) before
going back to the previous images.

### G2. Geo maps still need a GNSS datum to be used — **open question**
After local → geo, robots without a datum cannot start a session on the map, and a hand-placed
session of such a robot cannot be placed again after its next restart (a geo map is placed by
the datum only, Q-U8). Options: accept manual / reloc placement on geo maps too, or keep "convert
back to local" as the answer.

### G3. Limits — **low**
- The georeference is as good as the user's anchor; nothing checks it against GNSS. A robot
  placed by hand and later by its datum shows the anchor error as a jump at its next restart.
- `POST .../place {"source": "datum"}` trusts the robot's stored datum; when
  `datum_after_unplace` is false it may be the previous run's (the client says so).
- A placed session of a robot whose first datum arrives only after the conversion is
  re-derived from that datum (the dispatcher treats a session without a datum as changed).
- Reconstruction results made before a conversion keep their old `crs` in `meta.json`
  (informational; the external service does no math with it). The service gets
  `crs.bearing_deg` for rotated maps (absent at 0).
- `PUT /maps/{id}/datum` on an empty geo map now also sets `geo.bearing_deg` from the datum
  (an `enu` datum: its grid convergence), so the frame and the display transform agree.

---

## I. Full audit 2026-10-09 (open)

Findings of the fresh full audit of 2026-10-09 (cloud_server + sati-client) that were **not** fixed in rounds 1/2. Line
numbers are approximate (as of that date); re-verify before citing. Client findings are in
`../sati-client/docs/AUDIT_BACKLOG.md` section **AF**. Items listed there as fixed in parallel with this write-up
(main.py field allowlists and status override, whole-status writes / clear-fault endpoint, the raw orchestrator proxy's
SLAM rules, `/api/createToken` grants, config centralisation, healthchecks / `.dockerignore` / `:latest` pins /
`check_health.sh`) are deliberately **not** repeated here. Each numbered point is referenced as `I<group>.<n>`.

### I0. Status of this section
- **Planned, next (not yet done):** `main.py` error contract: the same failure answers 400/404/500 by route,
  `detail=str(e)` leaks internals, and `if service is None: 503` is repeated 55 times next to `_require_service()`.
  One mapping from exception to status plus one `_require_service()` dependency. [medium]
- **Accepted 2026-10-09: no users yet:** the LiveKit token and user are cached in browser storage (AsyncStorage /
  localStorage), and an anonymous LiveKit user falls back to the admin room. Revisit before the first external user
  (client side: AF in the client backlog).

### I1. mission-dispatch

1. [medium] MQTT intake is one serial loop; unknown-robot path awaits a DB get_object inline (server.py ~4904-4957) — slow DB stalls intake for all robots.
2. [medium] Datum messages re-read the open session (JOIN) every time even when unchanged (server.py ~2058-2092, 2292).
3. [medium] `_process_datum_message` dereferences `_robot_object.datum` with no None check (server.py ~2058); approx-position path guards it.
4. [medium] PostgresWatcher reconnects every 60 s quiet timeout and re-yields every row → all missions/robots reprocessed + "Update a RUNNING mission" INFO log per minute (postgres.py ~191-300, server.py ~1662).
5. [low/medium] `_foreign_error_refs` never cleared (server.py ~3819).
6. [low/medium] `get_mission_errors` hard-codes node ref keys instead of `_NODE_REFERENCE_KEYS` (server.py ~3825; also 3485, 3510).
7. [low/medium] `_fail_missions_of_deleted_robot` and queued-cancel branch mutate status.state directly, bypassing `_set_mission_state` (no failure category, recorder run_finished, events, `_remember_finished`) (server.py ~3229, ~1810).
8. [low/medium] Watcher reconnect period fixed 100 ms with WARNING each time during DB outage (postgres.py ~43, 155-166).
9. [low/medium] Four near-identical watch loops with two retry policies (`_watch_settings`, `_watch_sites`, `_watch_site_assignments`, `_watch_changes`).
10. [low] `_start_wait` float(action_parameters["seconds"]) can raise on every state message without failing the mission (server.py ~4056).
11. [low] Unused imports (fleet_recorder Iterable/RecordingLevel/ASSIGNMENTS_CHANNEL re-export used by server.py), duplicate `time` imports, inline PostgresDatabase re-import.
12. [low] behavior_tree.py: `is_order` naming inverted; print() instead of logging; unknown node types/None constants silently dropped; node type sets enumerated in 3 places.
13. [low] Naive local datetimes for mission timestamps vs aware UTC elsewhere (hidden by TZ=UTC). (Already noted in dispatch-audit follow-ups.)
14. [low] getattr(..., default) fallbacks for test doubles (`_writer_id()` returns fresh uuid4 when missing → defeats echo suppression).
15. [low] Per-state allocations in fleet_recorder.on_state; 5 re.match with formatted patterns per MQTT message on paho thread.
16. [low] Notify/charging webhooks via `requests` in default executor; notify URL from mission spec = SSRF egress; `_charging_mission_received` never times out.
17. [low] Conninfo strings built by hand with password (breaks on spaces/quotes); password as CLI arg (visible in ps). Use psycopg.conninfo.make_conninfo.
18. [low] `_blocked_node_tasks` / `_run_header_task` not cancelled in shutdown(); `asyncio.get_event_loop()` inside coroutines.
19. [low] Spliced/misplaced comment above `_finished_missions`/`_loop_errors` (server.py ~520); `_robot_online_task` TimerHandle-then-Task.

### I2. API: maps / sessions

1. [medium] Per-robot lock held across a whole awaited SLAM save on replace-start (maps.py ~2404, 2766; up to ~22 min). Server already has deferred start-after-save: make replace use wait=False + deferred start; then drop client nginx 240 s location + 504 recovery (sati-client nginx.conf ~128-152, utils/mapFinish.ts).
2. [medium] robot_delete: rosbag delete failure after sessions closed leaves robot half-deleted (robot_delete.py ~135-151).
3. [medium] MappingSwitch.forget() clears only _slam_state/_prev_intent; caches/locks/OrchestratorMaps caches leak per deleted robot (mapping_switch.py ~480).
4. [medium] robot_actions `service` mixes ids and display text; failure labels use raw names, success labels pretty names (mapping_switch.py ~233-256, 846-861).
5. [medium] Response key drift: robot_notified / mapping_warning / slam_warning / warnings / mapping_service (maps.py ~2060, README ~273).
6. [medium] orchestrator_maps held()/stored() fetch /maps/list up to 3x with separate caches (orchestrator_maps.py ~67-137, 182-203).
7. [medium] "no address/offline" guard copied 3x (orchestrator_maps.py ~112, 184; mapping_switch.py ~921); `cloud-<id>` prefix parsing twice (localization_view.py ~41, orchestrator_maps.py ~210).
8. [medium] notify_robot re-reads session+robot and fresh snapshot after every session change (maps.py ~2022-2067, 2227-2244).
9. [medium] Reloc jobs and inflight maps only in memory; API restart mid-reloc leaves intent changed, never rolled back (reloc_job.py ~243, 330).
10. [low] Retry/slow decisions by matching orchestrator error text (mapping_switch.py ~213, 686).
11. [low] Save poll budget counts sleep only, not call time (mapping_switch.py ~687-706).
12. [low] `_prev_intent` recorded only after PUT slam succeeds (timeout case) / not on ALREADY_RUNNING (mapping_switch.py ~543-552).
13. [low] reloc rollback runs twice on _Fail path (reloc_job.py ~395-414).
14. [low] localization_view.intent_view handles "older orchestrators" though spec says no fallback (localization_view.py ~80); stale section header orchestrator_client.py ~175.
15. [low] RESERVED_NAMES / load_map GEO/LOCAL shim (maps.py ~161, server.py ~809); README ~151 documents /map/load with GEO/LOCAL.
16. [low] README says robot_actions "always present" — code adds it only when actions is not None (maps.py ~2061, robot_delete.py ~166).
17. [low] `_watch_run_changes`: no per-robot dedupe of restart_after_run_change; missed run changes after API restart never reconciled (server.py ~2253).
18. [low] slam_save_state.py survives missing table by per-call catch only.

### I3. API core / database

1. [medium] WebSocketManager.broadcast sends serially, no timeout; a stalled client blocks `_handle_robot_updates` for all robots; unbounded `_robot_changes` queue; no coalescing (server.py ~139-163, 2036).
2. [medium] robot_update message built before checking subscribers; 60 s resync rebuilds all (server.py ~2285-2349).
3. [medium] mission_update / robot_update payloads hand-built twice (main.py ~2700, server.py ~2379, 2311); dead hasattr guards; both omit task_status/node_status/held/held_reason (backlog C4).
4. [medium] diagnostics.py per-robot caches keyed by MQTT topic name, never evicted, unknown names accepted (diagnostics.py ~62); diagnostics routes answer 200 null for unknown robots.
5. [medium] list_missions returns all missions unpaginated with full trees; list_robots SELECT * (main.py ~2344).
6. [medium] get_image without size calls blocking MinIO on the event loop; image errors swallowed → 404 (server.py ~1443-1453).
7. [medium] get-or-create swallows DB errors as not-found (main.py ~1873, 1103).
8. [low/medium] /stats exposes internal URLs; `/` route list stale (main.py ~396, server.py ~749).
9. [low/medium] names_index duplicates PK index; mission_trajectory created in code and migration; trajectory index doesn't match fleet_reads filter; no retention (postgres.py ~92-101).
10. [low/medium] cause codes seeded in migration and defined in events/causes.py separately.
11. [low] Dead/duplicate routes: status projections, /map/load, archive/restore vs PATCH, PUT /robots/{r}/map (410, scheduled removal).
12. [low] diagnostics `_schedule` discards run_coroutine_threadsafe future; WS handlers connect before validating target.
13. [low] create_bag_upload_url accepts any robot_name; DELETE /rosbags/{robot} deletes prefixes without ownership check.
14. [low] idempotency middleware buffers whole body uncapped.
15. [low] postgres.py logs + print_exc + re-raise (triple logging); create_object logs full spec at INFO.

### I4. Services / infra / tests

1. [medium] DependencyHealthChecker timeout ineffective (ThreadPoolExecutor shutdown(wait=True)) (service_utils.py ~105-121).
2. [medium] Arango failures swallowed into []/None (graph_db/server.py ~791, 840, 893) → planner says "no path" during outage.
3. [medium] agent_orchestrator: Anthropic client no timeout/max_retries; unbounded to_thread summarize per event batch; anthropic unpinned.
4. [medium] graph_builder: worker-thread race on session_to_global_map/stats; cleanup loop unguarded (server.py ~489, 541, 1367).
5. [medium] graph_builder `_check_robot_exists` treats DB error as missing → may create duplicate; known_robots never invalidated (server.py ~1285-1312).
6. [medium] MinIO list_buckets ×4 per client at construction, no HTTP timeouts; _delete_bucket lists all objects into memory (minio_base.py).
7. [medium] mqtt_client: only first matching callback runs; loop_stop before disconnect; watchdog reconnect without lock (mqtt_client.py ~134-168).
8. [medium] Test gaps: service_utils, MQTTClient routing/watchdog, graph_db (integration only), telemetry_sender, livekit main.
9. [low/medium] Floating pins: minio, python-arango, requests, websockets, httpx; orphan requirements files (topomap_dbs/graph_db, database); uvicorn variants; Dockerfile.unit httpx<0.28 vs prod newest; tests/requirements-test.txt pydantic<2 contradicts CLAUDE.md. → one constraints.txt.
10. [low/medium] livekit_sfu_tokens ROLE_GRANTS robot == operator; robot identity reuse can evict another robot.
11. [low] graph_db INFO logs of 3 nodes per call with emoji; planner get_node per waypoint.
12. [low] Per-client httpx.AsyncClient (no shared pool).
13. [low] Copy-pasted service boilerplate (main.py argparse/logging/health), duplicate StatsResponse, duplicate UpdatePublisher (graph_builder/agent).
14. [low] graph_builder create_task results not stored/cancelled (main.py ~125).
15. [low] Containers run as root; python:3.10-slim not digest-pinned.
16. [low] pytest.ini env_files needs pytest-dotenv; --showlocals noise; 84 unit tests unmarked.

### I5. Cross-repo seam (server half; client half in the client backlog AF)

1. [medium] No client caller for GET/DELETE /maps/{id}/blocked-nodes — operators can't see/clear blocked nodes.
2. [medium] Live node_added lacks session_id/timestamp (graph_builder server.py ~993) → session highlight misses live nodes.
3. [medium] Map list nests status.{state} but GET /maps/{id} returns flat; client normalizeMapSummary handles both; ~49 "older server" shims in client.
4. [low] mapping_state_update pushed only for TOPO, never carries mapping_services.
5. [low] Map display names: session.map is id only; client derives labels in two places (utils/mapLabels.ts ~16, utils/mapWindow.ts ~79).
6. [low] Server routes without client caller: PUT /maps/{id}/datum, POST sessions/{sid}/unplace, POST /navigate/waypoints, GET /health/recording, /detection_results*, GET /base_models/{id}/download-url, GET /rosbags, /stats — mark robot/ops-only in README or drop.
7. [low] Client deletes one robot's bags on one map by listing all + filtering + deleting one by one (mapApi.ts ~1089, 1121) — server lacks map filter.

### I6. Observations from the 2026-10-09 deploy
- graph-builder `/health` reports `mqtt_connected: false` although it is subscribed (see D5).
