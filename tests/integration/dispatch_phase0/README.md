# WP6 dispatch integration test

`run.sh` checks mission-dispatch's Phase 0 recording end to end
(docs/satinav-fleet-agent-phase0-v2.md §5.3, §7 WP6 item 7) on a throwaway stack:
TimescaleDB (`timescale/timescaledb-ha:pg17.11-ts2.30.1`, migrated with
`alembic -c packages/api/alembic.ini upgrade head`), a separate mosquitto, the
dispatcher, and the dummy robot in goal mode.

1. A mission runs to COMPLETED: one `mission_runs` row (level, passes, tree, immutable once
   terminal), `MISSION.RUN_STARTED`/`RUN_FINISHED`, `robot_state_ts` rows carrying the run
   id (the global level is set to `full`), `ROBOT.STATE_CHANGED`, `robot_latest`; a RUNNING
   run left by a "previous dispatcher" is closed `ABORTED`/`DISPATCH.ORPHANED` at startup.
2. A synthetic, cyclic sequence of `state`/`connection` messages is published twice; the
   second pass must not add a single event (deterministic ids + `ON CONFLICT DO NOTHING`).
3. Dispatch is restarted while the synthetic robot and the dummy robot keep streaming; no
   event may appear across the restart (rehydration from `robot_latest`, heartbeat grace).

Everything runs on a private `--internal` docker network with no published ports and
capped memory, so it does not touch services using host networking. All containers and the
network are removed on exit.

```bash
docker build -t wp6-test:py310 -f - . <<'DOCKERFILE'
FROM python:3.10-slim
COPY packages/controllers/mission/requirements.txt /r.txt
RUN pip install --no-cache-dir -r /r.txt "python-arango>=7.9.0,<8.1" "minio>=7.2.0,<7.3" \
    pytest==7.4.4 pytest-asyncio==0.21.1 alembic==1.13.3 SQLAlchemy==2.0.36 Pillow==10.4.0 httpx==0.27.2
ENV PYTHONDONTWRITEBYTECODE=1
DOCKERFILE
timeout 600 tests/integration/dispatch_phase0/run.sh wp6-test:py310
```

The same image runs the unit tests, one file per short-lived, memory-capped container:

```bash
docker run --rm --memory=2g --memory-swap=2g --network none -v "$PWD":/src:ro -w /src \
    -e ARANGO_PASSWORD=x -e MINIO_ACCESS_KEY=x -e MINIO_SECRET_KEY=x -e POSTGRES_PASSWORD=x \
    wp6-test:py310 timeout -s KILL 150 python -m pytest -p no:cacheprovider -q tests/unit/test_fleet_recorder.py
```
