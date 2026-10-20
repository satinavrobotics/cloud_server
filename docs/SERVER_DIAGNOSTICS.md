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

## Memory, the host and the active checks

- Every running service reports its **memory** (working set, without reclaimable file cache) against its limit; 80% of the
  limit is a warning, 92% an error, before the kernel kills it. A service without a limit shows its usage only.
- The **host** block gives total and available memory, swap, free disk and load; low memory (10%/5%), a swapping machine, a
  nearly full disk and an overloaded CPU are host findings. Two more: **container memory limits that add up to more than 110% of
  the machine's memory** (`host-memory-overcommitted`: when several peak together the kernel kills one at random) and, on a
  Raspberry Pi, **power throttling** (`host-power-throttled`, read from the firmware flags in `/sys/devices/platform/soc/soc:firmware/get_throttled`,
  the same as `vcgencmd get_throttled`; under-voltage is an error).
- **Checks** ask the running server the questions whose wrong answer has hurt before (see `checks.py`): the gateway
  answers and reaches the API, the video token service and the agent (a gateway pointing at a wrong address answers 502 for
  that route and logs nothing until someone uses it); what `server-info` advertises is what runs (and the other way round);
  the platform's own services run one image tag. `DIAGNOSTICS_GATEWAY_URL` (default `http://127.0.0.1:80`; the robot overlay
  uses `ROBOT_GATEWAY_PORT`) says which gateway to ask.
- `GET /services` carries all of it: `severity` (worst of everything), `summary.checks_failing`, `host_stats`,
  `host_findings`, `checks` and `services[].memory`.

## Deploying

Cloud: `docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build server-diagnostics`, then recreate
`nginx-gateway` (new route) and `api-delegation-service` (new capability). Robot: build and push the image
(`scripts/build_push.sh`), pull it on the Pi, set `SERVER_DIAGNOSTICS_AVAILABLE=true`, redeploy with
`scripts/deploy_robot.sh` (it recreates the gateway too).
