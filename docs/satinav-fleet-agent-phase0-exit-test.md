# SatiNav Fleet Agent — Phase 0 exit test (runbook)

The Phase 0 exit test (docs/satinav-fleet-agent-phase0-v2.md, top of the file, §8 and WP13):
five real runs on the simulated robot **`masked-frigatebird`**, then a read-only checker over
what was recorded, then a team review of the timelines.

- **Runs:** one normal completion, one forced failure, one disconnect mid-run, one cancel, and one
  at recording level `full` whose level changes mid-run.
- **Checker:** `tools/phase0_exit_check.py` implements §8 and WP13 as SQL checks and prints a
  PASS/FAIL table plus JSON. It also records the baseline share of `UNKNOWN` causes.
- **Out of scope:** the robot-side items of §8 (GNSS fix data in diagnostics, a stable build ID, a
  synced clock) are deferred to the robot-side step (WP3). The checker allows `sw_version` null and
  ignores GNSS, and nothing below depends on the robot's clock being synced.

> **Where this runs.** Only the five runs touch the live system, and only the sim robot.
> The checker is read-only: one `REPEATABLE READ, READ ONLY` transaction with the session default
> `default_transaction_read_only = on`.
> Steps marked **DISRUPTIVE** change something beyond "a sim robot drives a mission". Do them
> only while nobody else is using that robot.

## 0. Preconditions (read-only)

```bash
API=http://localhost:8000
R=masked-frigatebird
cd ~/satinavrobotics/cloud_server
COMPOSE="docker compose -f docker_compose/mission_dispatch_services.yaml"
APIC=$($COMPOSE ps -q api-delegation-service)

# WP13 deployed (~/pg-cutover/scripts/wp13.sh) and healthy: status "ok", no alerts,
# processes.api.role "writer", processes.dispatch present and not stale
curl -s $API/api/v1/health/recording | python3 -m json.tool | head -40

# the robot: online, IDLE, nothing running for it
curl -s $API/api/v1/robots/$R | python3 -c "import json,sys; r=json.load(sys.stdin); \
  print(r['name'], r['status']['state'], 'online' if r['status']['online'] else 'OFFLINE', \
  'map', (r.get('session') or {}).get('map'), 'pose', r['status']['pose'], 'level', r.get('telemetry_recording'))"
curl -s $API/api/v1/missions | python3 -c "import json,sys; \
  print([m['name'] for m in json.load(sys.stdin) if m['robot']=='$R' and m['status']['state'] in ('PENDING','RUNNING')])"

# its site (§8 wants a site on every run): must show an open assignment
curl -s $API/api/v1/robots/$R/site-assignments | python3 -m json.tool | head -20
# none? create one and assign it (records a robot_site_assignments row; not disruptive):
#   curl -s -X POST $API/api/v1/sites -H 'Content-Type: application/json' -d '{"name":"sim-lab","display_name":"Sim lab"}'
#   curl -s -X PUT $API/api/v1/robots/$R/site -H 'Content-Type: application/json' -d '{"site_id":"sim-lab"}'

# the robot's effective recording level now (note it: run 5 changes it and restores it)
curl -s $API/api/v1/robots/$R/recording

# mark the start of the test window (UTC, with zone)
T_START=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo $T_START
```

The heartbeat timeout decides when the disconnect is detected. It is the robot spec's
`heartbeat_timeout`, 30 s by default; check it in `GET /api/v1/robots/$R`.

### Mission helper

Missions go through the existing `POST /api/v1/missions`. The body needs `name`, `robot` and
`mission_tree`, plus an optional `timeout` in seconds (default 300). Build waypoints from the
robot's current pose, so they are on its map:

```bash
mission() {  # mission NAME TIMEOUT_S DX1 DX2 : a route of two waypoints DX1 / DX2 metres along x
  python3 - "$1" "$2" "$3" "$4" <<'PY' | curl -s -X POST $API/api/v1/missions -H 'Content-Type: application/json' -d @-
import json, sys, urllib.request
name, timeout, dx1, dx2 = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
r = json.load(urllib.request.urlopen("http://localhost:8000/api/v1/robots/masked-frigatebird"))
p = r["status"]["pose"]; m = ""  # mapless waypoints: robot frame (maps U6: no current_map)
wp = lambda dx: {"x": p["x"] + dx, "y": p["y"], "theta": 0.0, "map_id": m}
print(json.dumps({"name": name, "robot": "masked-frigatebird", "timeout": timeout,
                  "mission_tree": [{"name": "go",
                                    "route": {"waypoints": [wp(dx1), wp(dx2)]}}]}))
PY
  echo
}
state() { curl -s $API/api/v1/missions/$1 | python3 -c "import json,sys; s=json.load(sys.stdin)['status']; print(s['state'], s.get('failure_reason'))"; }
runs()  { curl -s "$API/api/v1/runs?robot=$R&limit=${1:-6}" | python3 -c "import json,sys; \
  [print(r['run_id'], r['mission_name'], r['state'], r['abort_cause'], r['recording_level'], r['site_id']) for r in json.load(sys.stdin)['items']]"; }
```

Adjust `DX` to distances the sim can drive (a few metres). Use a new mission name for every
attempt: names are unique.

## 1. Run 1: normal completion (moves the sim robot)

```bash
mission exit-1-complete 300 2 4
watch -n 2 "curl -s $API/api/v1/missions/exit-1-complete | python3 -c \"import json,sys; print(json.load(sys.stdin)['status']['state'])\""
runs 1
```

**Observe.** The mission reaches `COMPLETED`. The run has `state COMPLETED`, `abort_cause`
null, `recording_level events_only` (unless the robot is set otherwise) and a `site_id`.
`GET /api/v1/runs/<run_id>` lists `MISSION.RUN_STARTED`, `ROBOT.STATE_CHANGED` (IDLE→ON_TASK→IDLE)
and `MISSION.RUN_FINISHED` (outcome COMPLETED).

## 2. Run 2: forced failure by mission timeout (moves the sim robot)

The run gets a timeout far shorter than the route takes. The dispatcher's watchdog marks it FAILED
with "Mission timed out" and sends a cancelOrder, so the run becomes `TIMEOUT` with cause
`DISPATCH.TIMEOUT`.

```bash
mission exit-2-timeout 15 20 40
sleep 25; state exit-2-timeout; runs 1
```

**Observe.** The mission is `FAILED Mission timed out` and the run is `TIMEOUT` /
`DISPATCH.TIMEOUT`. `RUN_FINISHED` carries `outcome TIMEOUT`, and the robot returns to IDLE.

**Alternative: an unreachable goal.** `POST /api/v1/missions` does not plan, so the waypoint goes
straight to the robot, e.g. `mission exit-2-unreachable 300 500 1000`. Whether that fails depends
on the sim robot reporting a navigation error:
- an error of type `noRouteError`, or text matching "unreachable", "no path" or "planning failed",
  gives `NAV.GOAL_UNREACHABLE`;
- anything else gives `UNKNOWN`, which is fine and counts toward the baseline.

If it just drives on, cancel it; that run does not count as the failure.

## 3. Run 3: disconnect mid-run (**DISRUPTIVE**: cuts the sim robot's link for ~60 s)

Start a mission long enough to outlast the outage, then cut the robot's MQTT link for about 60 s.
That is longer than the 30 s heartbeat timeout.

```bash
mission exit-3-disconnect 900 3 6
sleep 5; state exit-3-disconnect    # RUNNING, robot ON_TASK
date -u +%T                         # note the cut time
```

Cut the link **for ~60 s**, then restore it. Pick one option.

- **A (preferred, robot side).** Stop the robot's MQTT bridge (its VDA5050 client) and start it
  again after 60 s. It runs on the robot and is managed by the robot's orchestrator. This repo
  does not define the orchestrator's routes; they are reachable through the existing proxy
  `/api/v1/orchestration/$R/...`. List them with a read-only
  `curl -s $API/api/v1/orchestration/$R/openapi.json` and use its stop/start (or restart)
  route for the bridge. Otherwise do it on the robot over Tailscale (`ssh`) with the process
  manager it runs under.
- **B (cloud host side).** Drop the robot's traffic to the broker. **This changes the host
  firewall; remove the rule afterwards.**
  ```bash
  IP=$(curl -s $API/api/v1/robots/$R | python3 -c "import json,sys; print(json.load(sys.stdin)['ip_address'])")
  PORT=$(grep -E '^MQTT_PORT_WEBSOCKET=' docker_compose/.env | cut -d= -f2)   # the robots' MQTT port
  sudo iptables -I INPUT -s "$IP" -p tcp --dport "$PORT" -j DROP
  sleep 60
  sudo iptables -D INPUT -s "$IP" -p tcp --dport "$PORT" -j DROP
  ```
  If the robots use plain TCP (`MQTT_PORT_TCP`), use that port instead.

**Observe during the outage** (read-only):
```bash
curl -s "$API/api/v1/events?robot=$R&code=ROBOT.HEARTBEAT_LOST&limit=3" | python3 -m json.tool | head -30
```
About 30 s after the cut there is one `ROBOT.HEARTBEAT_LOST`, carrying the run's `run_id`, and
the robot shows OFFLINE in `/api/v1/robots`.

After the restore there is one `ROBOT.HEARTBEAT_RESTORED` whose `gap_s` is about the outage length.
The mission then carries on or ends; either is fine, just note the outcome. If the robot does not
reconnect by itself within a minute, restart its bridge (option A).

`GET /api/v1/health/recording` should stay `ok` throughout. Losing one robot does not affect the
recorders. A `heartbeat_sweep_lag` or `report_stale` alert here would itself be a finding.

## 4. Run 4: cancel (moves the sim robot)

```bash
mission exit-4-cancel 900 3 6
sleep 8
curl -s -X POST $API/api/v1/missions/exit-4-cancel/cancel; echo
sleep 10; state exit-4-cancel; runs 1
```

**Observe.** The mission is `CANCELED`, and the run is `CANCELED` with cause `OPERATOR.CANCELED`.
The robot stops and returns to IDLE.

## 5. Run 5: level `full` with a change mid-run (moves the sim robot; writes time series)

```bash
curl -s -X PUT $API/api/v1/robots/$R -H 'Content-Type: application/json' -d '{"telemetry_recording":"full"}'; echo
sleep 2; curl -s $API/api/v1/robots/$R/recording; echo      # level full, source robot
mission exit-5-full-change 900 3 6
sleep 30                                                     # >= 30 s recorded at full
curl -s -X PUT $API/api/v1/robots/$R -H 'Content-Type: application/json' -d '{"telemetry_recording":null}'; echo
# let the run go on for >= 30 s after the change, then end it:
# let it complete, or: curl -s -X POST $API/api/v1/missions/exit-5-full-change/cancel
```

**Restore** the level the robot had in step 0. Setting `null` means "inherit". If the robot had its
own level, PUT that value back.

**Observe.** The run has `recording_level full`. Its timeline
(`GET /api/v1/runs/<run_id>/timeline`) shows:
- `recording.segments` = `full` up to the change, then `events_only` (or whatever the robot
  inherits);
- `recording.not_recorded` = one interval from the change to the end with
  `missing: ["time_series"]`;
- `tracks.robot_state.points` only before the change (±1 s);
- the `TELEMETRY.RECORDING_CHANGED` event in `recording.changes`.

Both PUTs write a `TELEMETRY.RECORDING_CHANGED` event, which is expected.

## 6. Run the checker (read-only)

Wait until every run is terminal, then run the checker inside the API container. Its image
ships `tools/`, and it reads the database settings the API uses.

```bash
docker exec $APIC python -m tools.phase0_exit_check --from $T_START --robot $R --json /tmp/phase0-exit.json
docker cp $APIC:/tmp/phase0-exit.json ~/pg-cutover/phase0-exit-$(date +%Y%m%d).json
```

- **Exit codes:** 0 = PASS (WARN/INFO allowed), 1 = at least one FAIL, 2 = could not run.
- **`--to`:** defaults to now.
- **`--no-scenario`:** leave out the five-run coverage requirement, e.g. to check production
  windows afterwards.
- **`--no-disconnect`:** a window without a LOST/RESTORED pair is then not a failure.

| Check | Fails when |
|---|---|
| `runs_in_window` | no run started in the window |
| `one_row_per_run` | a dispatched mission (missionobjectv1 with a run id) has no `mission_runs` row, or one mission has overlapping rows |
| `terminal_immutable` | the immutability trigger is missing/disabled, or a terminal row no longer matches its `RUN_FINISHED` (outcome, cause) |
| `required_fields` | a run lacks `site_id`, a valid `recording_level`, or (non-COMPLETED) `abort_cause`. `sw_version` null is allowed and counted (robot-side step deferred). RUNNING runs are a WARN |
| `run_events_once` | `RUN_STARTED`, or `RUN_FINISHED` on a terminal run, is missing or appears twice (not expected while the level was `off`) |
| `no_duplicate_event_ids` | an `event_id` is stored twice |
| `no_duplicate_logical` | a paired code repeats the same side back to back (LOST/LOST, ONLINE/ONLINE, RAISED/RAISED for the same error or alert, ...), a `STATE_CHANGED` transition repeats, or `NODE_FAILED` appears twice for one run and node: the "spurious event after a restart" signature |
| `timeseries_only_full` | `robot_state_ts`/`diagnostics_ts` rows exist while the robot's effective level (rebuilt like the timeline does) was not `full`; ±5 s around each change is allowed |
| `timeline_not_recorded` | a run's timeline segments do not cover its window, its `not_recorded` does not follow from the segments, or events / raw points show up inside an interval that says they were not recorded (a WARN when the level history disagrees with the run's stored level) |
| `heartbeat_pairs` | a `HEARTBEAT_LOST` has no `RESTORED` (WARN if the robot is still lost now), or no pair at all |
| `unknown_cause_share` | never: INFO. The baseline is `UNKNOWN` / non-COMPLETED terminal runs, and also over all terminal runs |
| `recorder_health` | dispatch or the API has not reported in 60 s, or an alert is active |
| `scenario_coverage` | fewer than 5 runs, or no COMPLETED, no FAILED/TIMEOUT, no CANCELED, no run with a LOST/RESTORED pair inside it, or no run started at `full` whose level changed |

On a FAIL, the table prints the first offending rows and the JSON (`details`) has up to 20. Fix
the cause, or re-run the affected scenario with new mission names and a new `--from`.

**Sample output** (from `tests/integration/phase0_exit_check/run.sh`, the same scenario seeded
through the real recording code on a throwaway TimescaleDB):

```
Phase 0 exit check  window [2026-09-26T20:33:53.561742+00:00, 2026-09-26T20:34:18.147541+00:00)  robot=all

CHECK                   STATUS  SUMMARY
----------------------  ------  -------
runs_in_window          PASS    6 run(s): 1 CANCELED, 3 COMPLETED, 1 FAILED, 1 TIMEOUT
one_row_per_run         PASS    6 dispatched mission(s) checked; 0 without a row, 0 overlapping duplicate(s)
terminal_immutable      PASS    trigger enabled; terminal rows match their RUN_FINISHED
required_fields         PASS    site, level and cause present (sw_version null on 6/6: robot-side step deferred)
run_events_once         PASS    6 run(s): RUN_STARTED/RUN_FINISHED exactly once
no_duplicate_event_ids  PASS    no event_id appears twice
no_duplicate_logical    PASS    16 event(s): no spurious repeats
timeseries_only_full    PASS    1 robot(s): no time series outside level full
timeline_not_recorded   PASS    6 timeline(s) consistent
heartbeat_pairs         PASS    1 LOST/RESTORED pair(s)
unknown_cause_share     INFO    UNKNOWN on 1/3 non-COMPLETED run(s) = 33% (1/6 of all terminal runs)
recorder_health         PASS    both processes reporting, no active alert (0 raised / 0 cleared in the window)
scenario_coverage       PASS    all exit-test runs present

OVERALL: PASS  (12 PASS, 1 INFO)
```

## 7. Team review and record-keeping

1. Open each run's timeline in the sati-client run view (or `GET /api/v1/runs/<id>/timeline`). The
   team agrees it shows the outcome, the cause, the relevant events and the coarse telemetry
   around them (the Phase 0 exit criterion).
2. Record in docs/satinav-fleet-agent-phase0-v2.md §8:
   - the date;
   - the checker's `OVERALL`;
   - the `baseline.unknown_cause_share_of_non_completed`;
   - the JSON path.

   Then tick "The exit test passes".
3. Keep the five runs: they are the reference set. Don't archive or delete them.

## Disruptive steps at a glance

| Step | Effect | Undo |
|---|---|---|
| Deploy WP13 (`~/pg-cutover/scripts/wp13.sh`) | API restart (seconds), dispatch restart (~10 s); only while no robot is `ON_TASK` | the script prints the rollback |
| Runs 1, 2, 4, 5 | the sim robot drives short missions | none needed |
| Run 3 option A | the sim robot's MQTT bridge is stopped ~60 s | start it again |
| Run 3 option B | host firewall rule dropping the sim robot's IP on the MQTT port | `iptables -D ...` (same rule) |
| Run 5 | robot level `full` for a few minutes, 2 `RECORDING_CHANGED` events | PUT the previous level back |
| Checker | none (read-only) | — |
