# Server diagnostics

A small read-only service that shows the dashboard which containers this server runs, whether they are healthy, what
their logs say and what looks wrong. It is the server's counterpart of the robot orchestrator's service logs.

- Code: `packages/services/server_diagnostics/` (own image, `scripts/build_push.sh` builds it). Port **8011**, bound to
  `127.0.0.1`; the gateway exposes it as `/api/diagnostics/`. Nothing but the gateway can reach it.
- `GET /services` (state, health, restarts, a severity and the number of findings per service),
  `GET /services/{name}/logs?lines=200` (max 2000), `GET /services/{name}/analysis?lines=500`, `GET /health`.
- Advertised as `capabilities.diagnostics` in `GET /api/v1/server-info` (`SERVER_DIAGNOSTICS_AVAILABLE`: on for a cloud
  server, off for a robot until the image is deployed there and the setting is `true` in `.env.robot`).

## What it can and cannot do

- It reads the Docker engine over `/var/run/docker.sock`, mounted **read-only** into the container, and sends `GET`
  requests only (list, inspect, logs). The socket still grants whatever the engine allows to whoever speaks to it, so the
  safety is in the code (no write call exists, container ids come from the engine's own list, never from the caller)
  and in the network (loopback only, behind the gateway, tailnet only).
- Every log line is scrubbed on the way out: passwords, secrets, tokens, API keys, JWTs, `Authorization` headers and
  credentials inside URLs become `[redacted]`. Best effort: a secret printed as free text cannot be recognized.
- Which containers: those with a compose service label, plus names starting `sati_`. `DIAGNOSTICS_INCLUDE` (a regex on
  the container or service name) replaces that default, e.g. to hide other projects on the same host.

## How problems are found

Only the last 15 minutes (`DIAGNOSTICS_WINDOW_MINUTES`) are judged, so a fixed problem stops showing.
`analyzer.py` holds the rules; each says what happened in plain words and what to check:
container state (stopped with an error, crash loop, memory kill, failing health check, restarted a moment ago) and log
patterns (connection refused, unresolved names, timeouts, refused logins, out of memory, full disk, port in use,
permission denied, database and MQTT trouble, a gateway with no working upstream, Python tracebacks, and a count of
error-level lines). Severity is `ok`, `warn` or `error`; a service's severity is its worst finding.

The nginx gateway also writes its error log to stderr now, so these rules can see it. This is the failure the first
robot test showed: a gateway sending the token route to `localhost:8008` while the service listened on the tailnet
address answers 502, and the log says `connect() failed (111: Connection refused) while connecting to upstream`.

## Deploying

Cloud: `docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build server-diagnostics`, then recreate
`nginx-gateway` (new route) and `api-delegation-service` (new capability). Robot: build and push the image
(`scripts/build_push.sh`), pull it on the Pi, set `SERVER_DIAGNOSTICS_AVAILABLE=true`, redeploy with
`scripts/deploy_robot.sh` (it recreates the gateway too).
