# mission-dispatch process lifecycle

Code: `lifecycle.py`, `RobotServer.run` / `graceful_shutdown` in `server.py`.

- **Single instance.** Before connecting MQTT the dispatcher takes a session-level Postgres
  advisory lock (`LEADER_LOCK_KEY`, derived from `mission_dispatch_leader`) on a dedicated
  connection. A second instance logs a warning and retries every 3 s (the old one may still be
  shutting down). If the lock connection dies the process exits with code 1 so docker restarts
  it.
- **Liveness.** A coroutine on the main event loop touches
  `$MISSION_DISPATCH_HEARTBEAT_FILE` (default `/tmp/mission_dispatch/heartbeat`) every 5 s, only
  if a `SELECT 1` through the pool succeeded in the last 15 s and MQTT is connected. The compose
  healthcheck fails when the file is older than 60 s (a wedged loop, a dead DB or a lost broker).
  Plain `docker compose` does **not** restart an unhealthy container (only swarm does); the
  status shows in `docker ps` and `scripts/check_health.sh`, and a restart must be triggered
  externally (e.g. a watchdog running `docker restart` on `unhealthy`).
- **Shutdown.** SIGTERM/SIGINT cancel the server tasks, then, in order and each step bounded
  on its own (so a slow one cannot starve the next): disconnect MQTT (no new work); shut every
  robot controller down (timers, notify and charging-hook tasks, message loop) and wait for the
  flush of its queued status writes (a mission's final state; up to 3 s); drain the fleet
  recorder's queued run writes (`_StartRun`/`_FinishRun`, up to 2.5 s; what does not fit is
  settled by the next start's orphan reconciliation); stop the recorder and close its pool;
  close the DB pool; release the leader lock. The whole is bounded at 13 s
  (`SHUTDOWN_TIMEOUT_S`), then exit 0. Compose sets `stop_grace_period: 20s` and `init: true`.
