# Navstack deploy: the server side (2026-10-27)

Implements section 4 of `web_stack/NAVSTACK_DEPLOY_SERVER_CLIENT_PLAN_20261027.md` except 4.5's data move (a live-host
operation). The robot half is the orchestrator contract in `satibot_orchestrator/docs/navstack-deploy-jetson-scope.md`
section 4 (owned there; this server only consumes it).

Code: `packages/api/navstack_images.py` (registry, fleet view, `parse_sw_version`), `packages/api/navstack_deploy.py`
(request model, store, job), `packages/api/orchestrator_client.py` (`image_*` calls), the guard in
`packages/api/orchestrator_proxy.py`, routes in `packages/api/main.py`, migration
`20261027_01_navstack_deployments`, retention script `sati_docker_registry/prune_navstack.py`.

## Routes

| Route | Answer |
|---|---|
| `GET /api/v1/navstack/images?branch=humble` | the builds of a branch in the registry (below); `502` with a `detail` when the registry cannot be read; `422` for a bad branch name |
| `GET /api/v1/fleet/software?branch=humble` | what each robot runs and whether it is behind `<branch>-latest` |
| `POST /api/v1/robots/{robot}/deploy` `{tag, mode: "image"\|"dev", pull_only?}` | `202 {"deployment_id"}`; `409 {"detail"}` when refused; `422` for a bad body; `404` unknown robot |
| `POST /api/v1/robots/{robot}/deploy/rollback` | `202 {"deployment_id"}`: back to the orchestrator's `previous_tag` / `previous_mode` |
| `GET /api/v1/robots/{robot}/deployments?limit=` | JSON list of deployments, newest first (default 20, max 200) |
| `GET /api/v1/deployments/{id}` | one deployment; `404` |
| `POST /api/v1/deployments/{id}/cancel` | the deployment; `409` once the switch has started or when it is over |

`X-Sati-Operator` (a request header, any text, trimmed to 200 characters) is stored as `requested_by` **as given and
unverified**: the API has no auth (user decision 2026-10-11), so anyone who reaches it can deploy.

### Shapes

`GET /api/v1/navstack/images`
```json
{"branch": "humble", "latest_tag": "humble-20261027-c879696",
 "images": [{"tag": "humble-20261027-c879696", "digest": "sha256:ab12...", "git_sha": "c879696...",
             "created": "2026-10-27T10:03:11Z", "commit_subject": "nav: fix corridor cost",
             "size_bytes": 9120000000, "aliases": ["humble-latest"],
             "build_info": {"git_sha": "c879696...", "commit_subject": "nav: fix corridor cost", "branch": "humble"}}],
 "fetched_at": "2026-10-27T18:30:00.123456+00:00"}
```
`digest` is the digest of the linux/arm64 image manifest. `latest_tag` is the versioned tag `<branch>-latest` points to
(null when there is none). Nothing pushed yet is an empty list, an unreachable registry is `502`, never an empty list.

`GET /api/v1/fleet/software`
```json
{"branch": "humble", "latest_tag": "humble-20261027-c879696", "registry_error": null,
 "robots": [{"robot_name": "formidable-peacock", "sw_version": "sati/navstack:humble-20261026-9497ec0",
             "mode": "image", "tag": "humble-20261026-9497ec0", "sha": "9497ec0",
             "last_seen": "2026-10-27T18:29:58+00:00", "behind_latest": true}]}
```
`registry_error` is an addition to the plan's shape: when the registry cannot be read the robots are still listed
(`latest_tag` null, nothing behind) and it says why; otherwise null. `behind_latest` is true only for image mode with a
tag of this branch that is not `latest_tag` (nor its alias `<branch>-latest`); dev builds, other branches and unknown
build ids are never behind. Only robots that have a `robot_latest` row are listed.

`parse_sw_version(s)` -> `{mode, tag, sha}`: `sati/navstack:<tag>` is `image` (sha = the 7+ hex tail of the tag), `dev:<describe>`
is `dev` (tag null, sha best effort), anything else null/null/null.

`POST .../deploy` -> `202`
```json
{"deployment_id": "5fb3b320-f71b-4389-ab38-ffc93441776d"}
```

Deployment (`GET /api/v1/deployments/{id}`, list items)
```json
{"id": "5fb3b320-...", "robot_name": "formidable-peacock", "kind": "deploy", "mode": "image",
 "to_tag": "humble-20261027-c879696", "from_tag": "humble-20261026-9497ec0",
 "from_sw_version": "sati/navstack:humble-20261026-9497ec0", "to_git_sha": "c879696...",
 "state": "pull", "error": null, "requested_by": "alice",
 "requested_at": "2026-10-27T18:30:00+00:00", "started_at": "2026-10-27T18:30:00+00:00", "finished_at": null,
 "steps": [{"name": "preflight", "state": "done", "started_at": "...", "finished_at": "...", "detail": "deploy to humble-20261027-c879696; robot runs humble-20261026-9497ec0"},
           {"name": "pull", "state": "running", "started_at": "...", "finished_at": null, "detail": "docker_pull: layer 3/9"},
           {"name": "switch", "state": "pending", "started_at": null, "finished_at": null, "detail": null},
           {"name": "verify", "state": "pending", "started_at": null, "finished_at": null, "detail": null}]}
```
`kind` is `deploy | rollback | pull`; `state` is `queued | preflight | pull | switch | verify | done | failed |
failed_verify | cancelled`; a step's `state` is `pending | running | done | failed | skipped`. A `pull_only`
deployment has the two steps `preflight`, `pull`. When a deployment ends the steps that did not run are `skipped`
and the failing step carries the error text. `to_tag` is always the versioned tag (an alias such as `humble-latest` is
recorded as the tag it points to); `to_git_sha` comes from the registry and, failing that, from the robot after the
switch. The orchestrator operation id of a pull / switch step is kept in the stored step (resume) and not returned.

## State machine

```
queued -> preflight -> pull -> switch -> verify -> done
              |          |        |         `-> failed_verify
              |          |        `-> failed
              `----------`-> failed | cancelled           (cancel only in queued, preflight, pull)
```
One row update per step; `state` is the step being run. Intervals and timeouts are config constants
(`packages/config.py`, env-overridable): robot online `NAVSTACK_ONLINE_MAX_AGE_S` 30; pull poll 3 s / timeout 45 min;
switch poll 3 s / timeout 10 min; verify poll 2 s / timeout 120 s.

1. **preflight** (at request time, so refusals are `409`; the row exists while it runs and is deleted when refused, so a
   refusal leaves no row). For deploy / rollback: the robot reported within 30 s (`robot_latest.last_seen`), no `RUNNING`
   mission run, no open *mapping* session (same store as the proxy's `slam_conflict`) and no SLAM recording / save
   (`MappingSwitch.slam_busy`); then for every kind: `GET /system/image` answers (404 -> `robot has no image mode`),
   no operation of the robot's running, the tag is in the registry (image mode), for deploy / rollback
   `capabilities.switch` is `true`, the target is not what already runs, and, when the tag is already local (or dev),
   `POST /system/image/switch?check=true`, whose `409 detail` is the refusal text **verbatim**. A tag that is not
   local is checked after the pull. `pull_only` skips the robot-busy guards (pulling while it works is the point)
   and refuses a tag that is already on the robot. Rollback reads the target from `previous_tag` / `previous_mode`.
   A second request for a robot with a non-final deployment is `409` naming it (also when two race: the partial
   unique index `navstack_deployments_one_running`).
2. **pull**: skipped when the tag is in `local_images` (and in dev mode). `POST /system/image/pull`, then
   `GET /system/image/operations/{id}` every 3 s up to 45 min; a dropped connection, timeout or 5xx during polling
   is retried until the timeout; a `404` operation is judged by `local_images`. The op's running step becomes the
   step's `detail`. `pull_only` ends here (`done`). There is no orchestrator call to cancel a pull: cancelling
   stops tracking, the pull finishes on the robot and the image stays there.
3. **switch**: first the guards and `switch?check=true` again (a pull takes minutes: a mission may have started),
   then one compare-and-set `pull -> switch` (the point of no return: a cancel that loses it is `409`), then
   `POST /system/image/switch {mode, tag}` (rollback: `POST /system/image/rollback`) and poll the operation (3 s, 10 min).
   **A dropped connection is not a failure** (including on the POST itself: the robot is then asked for its
   `active_operation` / `running`, and the POST is repeated only if it did not take it). Operation `failed`,
   `rolled_back` (the robot rolled itself back), `interrupted` or a timeout -> `failed` with the robot's text; a `404`
   operation -> verify decides.
4. **verify**: `robot_latest.sw_version` matches the target (`sati/navstack:<tag>` / any `dev:...`) **and**
   `GET /system/image` `running` agrees, within 120 s, else `failed_verify`. The server never rolls back by itself
   (the robot already does after a failed health check; the operator can `POST .../deploy/rollback`).

**Restart.** Rows in a non-final state are resumed when the API starts (`NavstackDeployments.start_resume`, lifespan in
`main.py`), one runner per row across processes (advisory lock). A resumed job continues from its state with the
stored operation id; without one it asks the robot for `active_operation`; a resumed switch asks the robot before it
POSTs again. A preflight that now refuses fails the row. A shutdown cancels the tasks and leaves the rows non-final.

## Proxy guard

`POST /api/v1/orchestration/{robot}/system/image/switch|rollback` is refused with `409 use POST
/api/v1/robots/{robot}/deploy` (dot segments and doubled slashes are normalised first), next to `slam_conflict`, so every
switch has a log row. Pull, prune and every read stay proxied. One addition to the plan: `switch` with exactly the
query `check=true` (the side-effect-free preflight the client dialog shows) passes; any other query is refused.

## Migration `20261027_01_navstack_deployments`

Follows `20261027_01_recordings` (the single head). Table `navstack_deployments` as in the plan (+ CHECKs on kind, mode,
state), the partial unique index `navstack_deployments_one_running (robot_name) WHERE state NOT IN (done, failed,
failed_verify, cancelled)`, index `(robot_name, requested_at DESC)`, and the view
`robot_software_periods(robot_name, mode, tag, git_sha, deployment_id, valid_from, valid_to)` over the `done` deploy /
rollback rows (`valid_from` = the row's `finished_at`, `valid_to` = the next such row's, null for the current one; the
plan's `(robot, tag, from, to)` with `from` / `to` renamed because they are reserved words). Joins fall back to
`recordings.image_tag` / `mission_runs.sw_version` for dev mode and for anything before the log. Downgrade drops the
view and the table with the log. Applied by the API container's entrypoint like the others.

## Registry reads

`NAVSTACK_REGISTRY_URL` (default `http://100.85.3.47:5050`, the host of `sati_docker_registry`), `NAVSTACK_IMAGE`
(`sati/navstack`), `NAVSTACK_ARCH` (`arm64`), `NAVSTACK_IMAGES_CACHE_TTL_S` (60). Async httpx port of
`navstack_versions.py`: index -> linux/arm64 manifest -> config labels; per-tag rows and config blobs cached for the
life of the process, `tags/list` and `<branch>-latest` refetched at most every 60 s, concurrent refreshes share one fetch.
The API container must reach the Tailscale address (it runs with host networking, like the registry client of the
host).

## What the Jetson side must provide

Exactly the contract of the Jetson document section 4, and in particular: `GET /system/image` with `running.{mode,tag,
git_sha}`, `previous_tag`, `previous_mode`, `local_images[].tag`, `capabilities.switch` (**the server refuses a deploy
unless it is `true`**) and `active_operation`; `POST /system/image/switch` (`?check=true` -> `200`/`409 detail`, real
call `202` op or `200 {noop: true}`) and `/rollback`; operations with `state` in `running|done|failed|rolled_back|
interrupted`, `steps[]`, `result.error`; `404` on every route when image mode is off; the factsheet republished
after a switch so `robot_latest.sw_version` becomes `sati/navstack:<tag>` / `dev:<describe>` within seconds (verify
needs mission-dispatch rebuilt with the factsheet `information` fix). Nothing of this exists on a robot yet: see the
gaps.

## Retention

`sati_docker_registry/prune_navstack.py` (own repo): dry run by default; keeps per branch the newest 10 images, every tag
a robot runs (Postgres `robot_latest` through `NAVSTACK_PRUNE_DSN`, or a JSON file), every tag in `navstack_deployments`
of the last 30 days and its `from_tag`, every `-latest` alias; deletes the others by digest only with `--apply` (which
refuses to run without robot data) and never runs garbage collection (it prints the stop / dry-run / collect / start
commands). The build cache is listed against a size limit, not deleted.

## Known gaps

- **Never run against a real orchestrator in image mode**: the Jetson switch / rollback / capabilities / steps do not
  exist yet. Everything is tested against a fake that follows the contract text; field names (`active_operation`,
  `result.error`, `previous_mode`, the op `state` values) are the first thing to check once J2-J4 exist.
- No auth: `requested_by` is unverified, anyone who reaches the API can deploy (plan risk 1).
- A mapping session or a mission can still start on the robot between the last guard check and the orchestrator's own
  switch preflight (seconds); the orchestrator's guards are the backstop. Starting a mapping session while a
  deployment runs is not refused by the server.
- Cancelling during a pull does not stop the pull on the robot (no orchestrator route for it).
- `verify` depends on `robot_latest.sw_version` being filled (mission-dispatch rebuilt with the factsheet fix); without
  it every deploy ends `failed_verify` although the robot switched.
- A resumed `switch` step without a stored operation id cannot tell "never sent" from "sent, id lost" except through the
  robot's `active_operation` / `running`; it asks first, which covers both unless the robot's op list is lost.
- `robot_software_periods.valid_from` is the verify time (up to ~2 minutes after the switch).
- A tag in the registry that the robot no longer has locally is pulled again; a tag deleted from the registry cannot
  be deployed (rollback targets that are still local are fine).
- 4.5 (moving the registry data to `/media/satiadmin/data0/docker_registry`) is a live-host operation, not done here.

## Deploy / rebuild

`api-delegation-service` rebuilt (new modules, routes, the migration; `restart_services.sh`); no other service changes.
New env (all optional): `NAVSTACK_REGISTRY_URL`, `NAVSTACK_IMAGE`, `NAVSTACK_ARCH`, `NAVSTACK_DEFAULT_BRANCH`,
`NAVSTACK_REGISTRY_TIMEOUT_S`, `NAVSTACK_IMAGES_CACHE_TTL_S`, `NAVSTACK_ONLINE_MAX_AGE_S`, `NAVSTACK_PULL_POLL_S`,
`NAVSTACK_PULL_TIMEOUT_S`, `NAVSTACK_SWITCH_POLL_S`, `NAVSTACK_SWITCH_TIMEOUT_S`, `NAVSTACK_VERIFY_POLL_S`,
`NAVSTACK_VERIFY_TIMEOUT_S`.

## Tests

`tests/unit/test_navstack_images.py` (fake registry, parser, fleet view, routes), `tests/unit/test_navstack_deploy.py`
(state machine on a fake orchestrator / store / clock, routes, migration), `tests/unit/test_orchestrator_proxy.py`
(guard), `tests/unit/test_orchestrator_client.py` (`image_*`), `tests/integration/test_navstack_deployments_postgres.py`
(opt-in, `NAVSTACK_TEST_DSN` = a scratch database whose name contains `scratch`: migration up/down/up, the partial unique
index, racing inserts, the store, the view, the job on the real store), `sati_docker_registry/tests/`.
