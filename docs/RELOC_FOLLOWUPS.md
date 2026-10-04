# Relocalization / SLAM-toggle — follow-ups (point of reference)

Written 2026-10-04 at the end of the session that built the SLAM toggle, the three-mode
relocalization (`packages/api/reloc_job.py`) and the manual unplace hook. Everything below
is **deliberately not done**. Line numbers are as of commit `e2c4467` + the uncommitted
unplace work; grep the symbol if they drifted. Design background: `satinav-maps-redesign.md`.

State of the feature: unit suite green (2596 passed). Live-verified on the sim stack
(Isaac Sim + navstack + `masked-frigatebird`): `POST .../place` mode 2 (Odin only), mode 3
(Odin + user pose, `init_pos` is the 7-vector), the 90 s timeout path, and
`POST .../unplace`. Not live-verified: `DELETE .../reloc-job` and the failure rollback
(unit-tested only). No browser/UI check of the placement UI or SLAM toggle has been done.

## A. Refactors (planned for a separate session)

| # | Item | Where | Notes |
|---|------|-------|-------|
| R1 | One shared `httpx.AsyncClient` | `api/orchestrator_proxy.py:116` (client per call), `api/orchestrator_client.py:100` (`http_factory`), `api/reconstruction_client.py:35` (own lazy client) | Each orchestrator call opens a fresh client/connection pool. Share one, close it on shutdown. Keep `http_factory` injectable: the unit tests use it. |
| R2 | **DONE 2026-10-04** Move `pick_service` out of `mapping_switch` (now `api/orchestrator_services.py`, no re-export needed) | def `api/mapping_switch.py:72`; used by `api/reloc_job.py:72,421` and `api/orchestrator_maps.py:26,95`, and in `mapping_switch.py:355` | Reloc code imports from the mapping switch only for this helper. Move to a small shared module (e.g. `api/orchestrator_services.py`) and re-export for one release if anything else imports it. |
| R3 | De-duplicate "place a session from a reloc result" | `api/reloc_job.py:~534` (job path) and `api/maps.py:~2236` (`place` with `source:"reloc"`) | Both call `ms.reloc_placement(robot_pose)` then write the placement/event. One helper, one place for the D0 frame question. |
| R4 | **DONE 2026-10-04** Single-flight `MappingSwitch.snapshot` (an invalidated in-flight read is not cached) | `api/mapping_switch.py:444` | N concurrent `GET /robots` each do a list call plus one sequential status call per service. `OrchestratorMaps.held` already shares an in-flight task: copy that pattern. |
| R5 | **Checked, not worth doing**: only `_start_error` (mapping_switch) and `describe_error` (reloc_job) overlap, ~3 shared lines with different wording; the proxy is a passthrough and the other sites are never-raise views. `maps.py` has no such mapping. | `api/orchestrator_maps.py`, `api/orchestrator_client.py`, `api/maps.py` (`reloc_status`, `map_reloc`) | The same "orchestrator unreachable / refused -> HTTP status + message" translation is written in more than one place. |
| R6 | Persist pending SLAM saves | `api/mapping_switch.py` (`slam_save_pending`), `api/maps.py:1856-1890` (`_slam_wanted`, `_start_slam`, `_save_slam`) | Finishing a SLAM session saves the Odin map in a background task. Robot offline at finish, or an API restart, loses the save: no persisted flag, no retry. Needs a durable "save pending" record + retry on robot reconnect. |

## B. Known gaps / future work

- **Jobs lost on API restart.** `RelocJobs` is in memory, one job per robot. A restart
  mid-job leaves `odin_reloc` running on the robot and the client sees 404 on
  `GET .../reloc-job`. Persist jobs, or on startup reconcile against the orchestrator.
- **D0: identity transform.** `utils/map_sessions.py:124-165` assumes the session frame equals
  Odin's map frame. `reloc_map_t_session()` is the single place to change
  (marked `TODO(D0)`). Needs navstack-team confirmation.
- **No real localization score.** Reloc "success" is `positionInitialized` plus a settle
  wait (`RELOC_JOB_SETTLE_S`); there is no quality score and `localizationScore` is not used
  to gate placement.
- **No map transfer robot <-> cloud.** Mode 2/3 placement needs the stored map on the robot's
  orchestrator. Placing a cloud map the robot has never held is refused with
  409 "does not hold a stored map". Seen live with `Hospital` on the sim robot, which is why
  it had to be placed by hand.
- **Persistent SLAM status indicator** in the client: only visible during the action today.
- **Unplace button in `sati-client`.** The API exists (`POST .../unplace`, a dev/test hook,
  see its docstring); no UI on purpose for now.

## C. Housekeeping

- `tests/requirements-test.txt` pin fixed to pydantic v1 (`>=1.10,<2`) on 2026-10-04.
- `.claude/worktrees/`: clean worktrees with no unmerged commits were removed (branches kept).
  Left alone: `agent-a2b116e924907faba`, `agent-a9997d26c68bc6e3a` (locked),
  `agent-a71cbf8c65d358c69` (6 uncommitted files), `agent-aff9f46e8fcf35c99` (1 unmerged
  commit), `agent-aa787ed42d734f39e` (partly removed, permission denied; likely root-owned
  files from a container: `sudo rm -rf` it, then `git worktree prune`).
- Client behaviour-tree test depends on `../deployment_ws` being present.
- Sim side (`/home/satiadmin/isaacsim/sim_arc`, uncommitted):
  `benchmark/config/orchestrator.sim.yaml` has `maps_dir: /tmp/sim_arc_maps`, an `odin_reloc`
  stub (`sleep infinity`) and `mapping: {service: sim_topomap, dummy: true}`. The sim needs
  them to run the reloc flow; commit or revert deliberately.
