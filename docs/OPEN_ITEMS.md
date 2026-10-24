# Open items — consolidated

Replaces the open parts of `AUDIT_BACKLOG.md` (audits of 2026-09-18 and 2026-10-09) and adds the
code-cleanup scan of 2026-10-24 (branch `feature/node-policies-v2`, HEAD 5a41ee0).
The desktop-board audit of 2026-10-23 is deliberately **not** in here.

Status of the triage: sections 1 and 2 were traced against the code. Items marked **untraced** were not
checked one by one and count as open until someone does. Line numbers are as of 2026-10-24.
Effort: S under an hour, M half a day to a day, L multi-day.

Client-side items (`../sati-client/docs/AUDIT_BACKLOG.md`) are not covered here.

---

## 1. Pick up now (small, mechanical)

| ID | What | Effort | Where |
|---|---|---|---|
| B1 (code) | Remove the LiveKit key/secret/URL literals used as `os.getenv` defaults; fail fast via `config.require_secret`; placeholders in `.env.example`. The comment cites a deleted "BACKLOG.md A3". | S | `packages/services/livekit/main.py:70-73`, `.env.example:115-117` |
| B2 (cheap part) | Orchestrator proxy: strip `Authorization`; validate the target against the robot record's IP. | S | `packages/api/orchestrator_proxy.py:32-34` |
| B3 (naming) | Compose falls back to literal default credentials; `.env.example` documents names compose does not read. Fix naming; keep or drop dev defaults per section 3. | S | `docker_compose/mission_dispatch_services.yaml:141,159-160,204,208-209`; `topomap_dbs/*/server.py` ctor defaults |
| C3 | Untrack `.coverage` and `coverage.xml` (add to `.gitignore`); drop the `POST /api/v1/explore` section from `docs/API_REFERENCE.md` (lines 98, 575, route does not exist). | S | repo root, `docs/API_REFERENCE.md` |
| C4 + I3.3 | Mission/robot WebSocket `*_update` payloads are hand-built twice and omit `task_status`/`node_status`/`held`/`held_reason`. One shared builder. | S-M | `packages/api/server.py:2481`, `packages/api/main.py:3460` |
| C6 (rest) | Whole-spec read-modify-write still in settings, map update and map datum PUT. Use `update_spec_fields` like the done items. | S-M | `api/main.py:1815`, `api/server.py:1015`, `api/server.py:1254` |
| C15 | Unknown `operatingMode` drops the whole state message. Make the enum tolerant (log once, keep the message). | S | `controllers/mission/vda5050_types/vda5050_types.py:571-577,629-633` |
| I0 | `main.py` error contract: one exception-to-status mapping; stop `detail=str(e)`. (`_require_service()` already adopted, 132 uses.) | M | `api/main.py:385,809,818,820` |
| I1.3 | `_process_datum_message` reads `_robot_object.datum` with no None guard. | S | `controllers/mission/server.py:2444` |
| I1.17 | Hand-built Postgres conninfo with the password; use `psycopg.conninfo.make_conninfo`. | S | `database/postgres.py:370` |
| H(g) | No `TZ` for mission-dispatch in compose; set `TZ=UTC` (ties to the naive-datetime item I1.13). | S | `docker_compose/mission_dispatch_services.yaml` |
| I4.1 | `DependencyHealthChecker` timeout is ineffective (`ThreadPoolExecutor` context manager waits). | S | `utils/service_utils.py:107` |
| D1 (health) | `/health` calls `health_checker.check_all()` synchronously on the event loop. | S | `api/main.py:427` |
| I4.3 | Anthropic client has no timeout or `max_retries`; `anthropic>=0.40.0` is unpinned. | S | `services/agent_orchestrator/agent.py:161`, its `requirements.txt` |
| I4.14 | `asyncio.create_task(...)` results not stored (tasks can be garbage collected). | S | `services/graph_builder/main.py:154-155` |
| I5.2 | Live `node_added` message lacks `session_id`/timestamp (user-visible highlight bug). | S | `services/graph_builder/server.py` |
| I3.7 | get-or-create swallows DB errors and hides them. Correctness. | S | `api/main.py:1741` (untraced) |
| I3.13 | Rosbag ownership check on bag routes. Correctness/security. | S | `api/main.py` bag routes (untraced) |
| I3.15 | `traceback.print_exc()` plus re-raise. | S | `database/postgres.py:514,536` |
| E | Delete the empty shell `packages/api/client.py` (no importers). | S | |
| E | Delete `plan_mission` / `_call_mission_planner` (definitions only, no callers; check tests first). | S | `api/server.py:1745,1780` |

## 2. Code cleanups (scan of 2026-10-24)

All verified by pyflakes or grep; nothing changed yet.

1. **Unused imports and locals, about 30 spots.** Mechanical, safe. Run `python -m pyflakes packages cloud_common` for the exact list. Main files: `api/server.py` (`threading`, `time`, `GraphDatabaseService`, `_uuid`, unused locals at 258 and 1400), `api/main.py` (`os`, `DISPATCHER_OWNED_STATUS_FIELDS`), `api/maps.py`, `api/semantic.py`, `api/map_events.py`, `api/mapping_stats.py`, `api/mesh.py` and `api/semantic.py` (imported but unused `_utcnow`), `services/livekit/main.py`, `services/server_diagnostics/operations.py`, `services/graph_builder/{scene_adapter,server,scene_memory}.py`, `utils/{mqtt_client,service_utils,fastapi_helpers}.py`, `database/postgres.py`, `topomap_dbs/*/server.py`, `scene_graph/{store,engine,morse}.py`, `telemetry_ingest/writer.py`, `controllers/mission/{server.py:4817,fleet_recorder.py}`, `api/reconstruction.py:1503`.
2. **Duplicated helpers.** `_utcnow()` is copied into 9 modules (`api/{mapping_switch,recorder_health,telemetry,map_delete,reconstruction,recording,maps}.py`, `telemetry_ingest/policy.py`, `controllers/mission/fleet_recorder.py` as `utcnow`). `_iso()`/`_parse_iso()` near-duplicates in about 8 `api` modules (bodies not yet compared). One shared helper in `packages/utils`.
3. **Debug prints.** Replace the two `print()` calls in `controllers/mission/behavior_tree.py:50,215` with logging. Same file defines `is_order` twice (lines 62, 90).
4. **Repo hygiene.** Decide whether `SPLAT_MESH_NOTES.md` at the repo root still belongs (content not read).
5. **Leave alone, probably intentional:** re-export `__init__` imports (`cloud_common/objects/__init__.py`, `services/livekit/__init__.py`, `services/mission_planner/__init__.py`), the star re-export in `vda5050_types/__init__.py`, `fleet_recorder` import in `controllers/mission/main.py` (side effects?), `ASSIGNMENTS_CHANNEL` (verify the `server.py` re-export first), `spatial_index_manager` print-based logger fallback.
6. **Existing TODOs (design notes, not cleanup):** `controllers/mission/server.py:320`, `cloud_common/objects/robot.py:35`, `utils/map_sessions.py:145` (D0 frame identity).
7. **Clean results:** no commented-out code, no `.orig`/`.bak` files, no dead undecorated functions found (lower bound).

## 3. Owner or design decisions (do not pick up unasked)

- **B1 rotation:** rotate the LiveKit key; its values stay in git history (the `.env` was untracked in `8590ea0`). History rewrite is optional.
- **B2 auth design:** API key/JWT with robot vs operator scopes; LAN-trust policy (`OPERATIONS_TRUST_LAN`, `b44e76f`). L.
- **B3:** `${VAR:?}` (breaks `docker compose up` on a fresh machine) vs keeping dev defaults.
- **H(e):** no watchdog for the mission-dispatch heartbeat healthcheck (autoheal container or systemd timer).
- **H(f), H(g), I1.13:** `deadline` is not enforced; needs naive vs aware time decided first.
- **C7:** what force-cancel does to a tracked mission. **H(c):** mission started then cancelled while the robot is offline blocks the queue.
- **I2.9:** reloc jobs only in memory (per `docs/RELOC_FOLLOWUPS.md`: only if it proves to matter). **I2.10:** retry decisions by matching orchestrator error text (needs error codes from the robot team).
- **I1.16:** notify webhook URL from the mission spec is an egress path (allowlist?). **I4.10:** LiveKit SFU `ROLE_GRANTS` robot == operator.
- **I3.5:** mission list pagination shape. **I3.8:** `/stats` exposes internal URLs. **I5.3:** map list nests status, `GET /maps/{id}` is flat. **I5.6:** server routes without a client caller (keep as ops API or drop).
- **F1** fleet-wide insights stream, **F2** robot status timestamp, **G2** geo maps need a GNSS datum.

## 4. Later (larger, or needs a test or hardware first)

- **D3** path planning minimises hops, not distance (no `weightAttribute`; edge key `distance` vs `weight`); needs a real-graph test. `graph_db/server.py:1371`.
- **D4** spatial queries are full collection scans (`spatial_index_manager.py`). **D1 rest:** blocking MinIO/Arango calls in API handlers.
- **C2 rest:** config defaults still outside `config.py` (do with B3).
- **C17** repeat/then-run/wait/multi-node completion untested on a real robot (needs a robot test).
- **I1.1, I1.2, I1.4, I1.5, I1.7, I1.9** (serial MQTT intake, datum re-reads, watcher resync every 60 s, `_foreign_error_refs` never cleared, bypassed `_set_mission_state`, four near-identical watch loops). Several untraced.
- **I2.1** robot lock across an awaited SLAM save on replace (untraced); **I2.2** robot half-deleted on rosbag failure; **I2.4**, **I2.15** (`GEO`/`LOCAL` shim in `api/maps.py:182,226`, `/api/v1/map/load`).
- **I3.1** `WebSocketManager.broadcast` serial with no timeout; **I3.4** diagnostics caches never evicted (`api/diagnostics.py:62-64`); **I3.9** `names_index` duplicates the PK index (needs migration); **I3.10** cause codes seeded twice.
- **I4.2** Arango failures swallowed into `[]`/`None`; **I4.4** graph-builder shared dict without a lock; **I4.5**, **I4.6**; **I4.7** `mqtt_client` runs only the first matching callback (`utils/mqtt_client.py:193-199`); **I4.8** test gaps; **I4.9** floating pins and no constraints file; **I4.15** containers run as root (none of 7 Dockerfiles has `USER`; test on the Pi first).
- **H(d)** headerId seed is clock-based and not persisted.
- **E** `GraphDatabaseService.update_node/delete_node/remove_node/find_path` (grep callers first); `scripts/update_robot_custom_actions.py` (targets a nonexistent port); duplication in argparse blocks, publishers, httpx boilerplate.
- **I7.1** adopt orchestrator `frame_id` (blocked on robot-side orchestrator); **I7.2** replace by a new SLAM session still does `then=stay`; **I7.3**; **I7.4** re-run `checks_use.py`/`checks_reuse.py` on the Postgres rig.
- **Untraced low items:** I1.10, I1.11, I1.14, I1.15, I1.18, I1.19; I2.5, I2.7, I2.8, I2.11-14, I2.16-18; I3.2, I3.6, I3.12, I3.14; I4.11-13, I4.16; I5.1, I5.4, I5.7. One lint pass and one review would cover most.

## 5. Drop or close in `AUDIT_BACKLOG.md` (already fixed or obsolete)

C1 (mitigated by `run_unit_tests_pinned.sh`), C8, C10, C11 (a failed-instant-action test is still missing), C12, C14, C16, C18 (cloud side; the rest is the robot team), D2, D5, D6 (watcher part), H(a), H(b), I2.3 (partly), I2.6, I6, E `_notify_graph_builder` (has callers now), I0's repetition part, and the I7 heading "(deferred)" is stale: frame witness is implemented in `dedfd54`. C13 is recommended to drop ("none found in normal operation"). G3 is informational.
