# Operator actions from the app (restart, deploy)

The server's diagnostics service (`packages/services/server_diagnostics`, behind the gateway at `/api/diagnostics/`) can restart this
server's containers, and on a robot deploy a tag, for the desktop app's Server health panel.

## Switching it on

`OPERATIONS_ENABLED=true` is in every profile (`docker_compose/profiles/*.env`). It does nothing until `OPERATIONS_TOKEN` is set in the
machine's `docker_compose/secrets.env` (random, 24+ characters; the example files list it) and `scripts/render_env.sh <profile>` is run.
Without a token the actions answer 403. The app asks for the token once per server and keeps it in its own storage.

## What it can do

| Call | What it does |
|---|---|
| `GET /operations` | enabled, kind (cloud / robot), can_deploy, recent actions. No token. |
| `POST /operations/restart` `{service, force}` | restart one service and wait until it is up (and healthy where it has a check) |
| `POST /operations/restart-all` `{force}` | every service, databases first, the gateway last; not the diagnostics service itself |
| `POST /operations/deploy` `{tag}` or `{rollback: true}` | robot only: `scripts/deploy_robot.sh <git sha>` or `--rollback` |
| `GET /operations/{id}` | state and log lines (secrets removed). No token. |

Posts need `X-Operations-Token`; `X-Operator` (free text) is logged. Each action returns 202 with an id at once; the app follows it.

Rules the server enforces, whatever the client sends:
- a service is named from the engine's own list; a tag must be 7-40 hex characters; nothing is ever run as a shell string;
- refused with 409 `missions_active` while any mission is RUNNING or PENDING, unless `force` (if the API cannot be asked, it goes ahead:
  it is often what is being fixed);
- one action at a time (409 `busy`); the diagnostics service is restarted only by a deploy;
- a cloud server answers deploy with 409 `not_a_robot`.

## Changing a robot's settings from the app

A robot server's tunable settings (memory limits of every service, the database caches, three scene-graph knobs) can be edited in the app's
Server health panel and applied without git or ssh.

| Call | What it does |
|---|---|
| `GET /operations/config` | the settings with their effective value, the profile's value, the default, which are changed locally, the services a change restarts, and `pending_apply` (saved but not running yet). No token. |
| `PUT /operations/config` `{changes: {KEY: "512m" \| null}}` | validates and saves to `docker_compose/local/overrides.env` on the machine (`null` removes the override). Saves only. Same auth as the other actions. |
| `POST /operations/deploy` `{apply_config: true}` | runs `scripts/apply_robot.sh`: render the env file again (profile + overrides + secrets), then `deploy_robot.sh` on the tag that runs now (or `{tag, apply_config}`). Refused while missions are active, like any deploy. |

Rules: only the settings listed in `robot_config.SETTINGS` can be changed, each with a type and bounds (a value is checked before it is written);
the memory limits together must not exceed 95% of the machine's RAM; secrets and identity (`SERVER_KIND`, addresses, passwords) cannot be reached.
`local/overrides.env` is per machine and not in git: the profile (`profiles/<name>.env`) stays the record of what the repo says, and
`CONFIG_HASH` covers only the profile, so a machine with local overrides still shows whether it is behind the repo; `CONFIG_OVERRIDES` is the
hash of the overrides (empty when there are none). To make a local change permanent for the machine, copy it into the profile and delete the
override. `PG_TUNE_*` is not offered: it is read once, when the database volume is first created.

## How a deploy runs

The deploy recreates the diagnostics service, so it cannot run inside it. The service starts a one-shot container from its own
image (which now carries the docker CLI and the compose plugin), with `DEPLOY_ROOT` (default `/cloud_server`) mounted at the same
path and the Docker socket, running `bash scripts/deploy_robot.sh <tag>`. That container is kept, with its log, for the last five
deploys (label `sati.operation=deploy`); the app reads its progress through `GET /operations/sati-deploy-<time>`. During the deploy
the service restarts and does not answer for a while; the app shows "waiting" and carries on. `deploy_robot.sh` still rolls back on
its own if the new tag is not healthy, and now writes the tag into `.env.robot`.

## Trust

The Docker socket is mounted `:ro`, which does not stop writes to it: whoever holds the token can restart anything on the server and
start a container with the repo mounted. The token and the tailnet are the whole barrier; the endpoints are not reachable outside the
tailnet. Keep the token in `secrets.env` only.
