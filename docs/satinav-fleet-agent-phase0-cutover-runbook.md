# Production cutover runbook: TimescaleDB / pg17 migration

**Scope of this window: image swap and data restore only.** No schema changes and no Alembic.
Those come in separate, later windows (see `docs/satinav-fleet-agent-phase0-v2.md` §7, WP1, and
§9 below). One change and one cause, if something breaks.

**Target:** `postgres:14.5` → `timescale/timescaledb-ha:pg17.11-ts2.30.1`. This is a deliberate
major upgrade. We do it once, together with the dump/restore that is happening anyway.

**Status: timed rehearsal done 2026-09-24 (§1–§7, including a real rollback) on a fresh staging
stack; see "Rehearsal log" at the end for what it changed.** Timings below are from that run.
The earlier compatibility rehearsal on the bridge-network staging stack (commit c5dbb4d) showed
that the migration itself is clean:

- the schema catalog diff against a pg14 baseline was empty and row counts matched;
- LISTEN/NOTIFY dispatch went `PENDING -> RUNNING` end to end;
- the test suite ran, and its 82 pre-existing failures (xfail in `tests/conftest.py`) are unrelated
  to Postgres.

That rehearsal did not measure how long each step takes, because that was not its purpose. Before
this runbook is used for the real cutover, rehearse it word for word on a **fresh** staging stack.
Use a fresh dump and reuse nothing from the compatibility check. Fill in the `[TIMING: ...]`
placeholders from that run. The database is small (about 10 MB on 2026-09-24), so the dump and
restore take seconds. Most of the window goes to stopping and starting services and running
verification. The rehearsal's main job is to confirm that this procedure is complete and in the
right order, not to save minutes.

> **Standing warning:**
> - ~~Nobody runs `restart_services.sh`, `docker compose down`, or `docker compose rm` on the
>   main stack.~~ **Lifted 2026-09-24**: §0 is deployed and verified, so `down`/`up` now
>   reattaches `pgdata14` by name.
> - **Nobody prunes Docker volumes** (permanent — see §0 "Leftover risk") (`docker volume prune`, `docker system prune --volumes`).
>   On Docker 29, `docker volume prune` removes unused *anonymous* volumes by default. That is
>   exactly the class of volume that holds production data today, plus the orphaned ones below.
> - **Do not delete the ~190 orphaned PG14 data volumes** (anonymous, dated 2025-10 → 2026-09).
>   They are very likely earlier production databases that were stranded by full restarts. A few
>   may be test leftovers. The owner has to decide whether any of them hold data worth recovering
>   before anything is removed.

## Shell setup (used by every section below)

Run this once per shell. Never `echo` these variables.

```bash
cd /home/satiadmin/satinavrobotics/cloud_server
COMPOSE="docker compose -f docker_compose/mission_dispatch_services.yaml"
PG=docker_compose-postgres-1
PW=$(grep -E "^POSTGRES_DATABASE_PASSWORD=" docker_compose/.env | cut -d= -f2)
USR=$(grep -E "^POSTGRES_DATABASE_USERNAME=" docker_compose/.env | cut -d= -f2)
DB=$(grep -E "^POSTGRES_DATABASE_NAME=" docker_compose/.env | cut -d= -f2)
CUTOVER_DIR=$HOME/pg-cutover/YYYYMMDD        # fill in the window's date. Explicit, no globs
mkdir -p "$CUTOVER_DIR"; chmod 700 "$CUTOVER_DIR"   # holds data dumps and rendered configs with credentials
psqlq() { docker exec -e PGPASSWORD="$PW" "$PG" psql -X -At -v ON_ERROR_STOP=1 -U "$USR" -d "$DB" "$@"; }
```

## 0. Pre-window change (separate deploy, before the cutover)

**Status: deployed and verified 2026-09-24 16:12.** Apps down ~20 s; the container
remounted the same volume (`b323568f…0fa52`); the before/after catalog + row-count snapshot
diff was empty (6 tables: 17 missions, 3 robots, 2 maps, 1 settings); all four services came
back with no errors, with 0 restarts. Artifacts are in `~/pg-cutover/20260924-prewindow/`.

### Problem

The `postgres` service in `docker_compose/mission_dispatch_services.yaml` declares no volume.
The container therefore only gets the `postgres:14.5` image's anonymous volume at
`/var/lib/postgresql/data`. The current one is
`b323568ff9d35df1ea2dfd6916624635ca53953379106159029ca3dd0830fa52`, created on 2026-09-04
together with the container (`docker_compose-postgres-1`).

`restart_services.sh` runs `docker compose down` and then `up -d`. `down` removes the container,
and compose cannot reattach the orphaned anonymous volume to the new one, so the new container
starts with a fresh, empty volume. The host has ~190 orphaned PG14 data volumes dated
2025-10 → 2026-09, so **each full restart very likely reset production to an empty database.**
This is not fully proven: a few of those volumes may be test leftovers.

This also breaks the old rollback plan. After the cutover recreates the container, nothing in
compose points at the old volume anymore.

### Change

Declare the existing volume as a named **external** volume, and mount it explicitly:

```diff
--- a/docker_compose/mission_dispatch_services.yaml
+++ b/docker_compose/mission_dispatch_services.yaml
@@ services:
   postgres:
     image: postgres:14.5
     environment:
       - POSTGRES_USER=${POSTGRES_DATABASE_USERNAME}
       - POSTGRES_PASSWORD=${POSTGRES_DATABASE_PASSWORD}
       - POSTGRES_DB=${POSTGRES_DATABASE_NAME}
       - POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256
     network_mode: "host"
+    volumes:
+      - pgdata14:/var/lib/postgresql/data
     healthcheck:
@@
 volumes:
   arango_data:
+  pgdata14:
+    # Production PG14 data. This was the container's anonymous volume, pinned by name so that
+    # `down`/`up` reattaches it instead of creating a fresh, empty one. external: compose never
+    # creates or deletes it, not even with `down -v`.
+    external: true
+    name: b323568ff9d35df1ea2dfd6916624635ca53953379106159029ca3dd0830fa52
```

- **Zero-copy.** The volume is the same one, mounted at the same path. No data moves. The only
  effect is that the container is recreated once, which is a short Postgres restart.
- **It stops the reset-on-restart problem.** After this change, `down` + `up -d` reattaches
  `pgdata14` by name. Because the volume is `external`, `docker compose down -v` does not delete
  it either.
- **It gives rollback a stable name to act on** (§7).
- Leftover risk: while no container references it (between a `down` and the next `up`), the
  volume still carries the `com.docker.volume.anonymous` label, so `docker volume prune` would
  still delete it. The no-prune rule in the standing warning stays in force permanently. It does
  not expire.

### Deploy and verify

Deploy this in its own short slot, under the same "no robot `ON_TASK`" pre-check as §1:

1. **Before editing the file**, record the current mount, the rendered config, and exact
   catalog and row counts. `snapshot` is defined in §2.3; paste that definition first.
   ```bash
   docker inspect "$PG" --format '{{json .Mounts}}' > "$CUTOVER_DIR/prewindow-mounts-before.json"
   $COMPOSE config > "$CUTOVER_DIR/compose-before.yaml"
   ```
2. Stop the Postgres-using services, in the §2.1 order, so that nothing writes. Then run
   `snapshot "$CUTOVER_DIR/prewindow-before"`.
3. Apply the edit, then diff the rendered compose config. The only differences should be the new
   `volumes:` entry on `postgres` and the top-level `pgdata14`:
   ```bash
   $COMPOSE config > "$CUTOVER_DIR/compose-after.yaml"
   diff "$CUTOVER_DIR/compose-before.yaml" "$CUTOVER_DIR/compose-after.yaml"
   ```
   `compose config` interpolates `.env`, so these files contain credentials. Keep them in
   `$CUTOVER_DIR` and delete them after review.
4. `$COMPOSE up -d postgres`, and wait for `healthy`. Then run
   `docker inspect "$PG" --format '{{json .Mounts}}'`. It must show `Type: volume`,
   `Name: b323568f…0fa52`, `Destination: /var/lib/postgresql/data`. That is the same volume as
   in step 1.
5. Run `snapshot "$CUTOVER_DIR/prewindow-after"`. Then
   `diff -r "$CUTOVER_DIR/prewindow-before" "$CUTOVER_DIR/prewindow-after"` must be empty.
   Counts and catalog are unchanged.
6. Start the services again (the §5 start order) and check their logs for clean Postgres
   connections.

Once this is verified, lift the `restart_services.sh` part of the standing warning.

## 1. Pre-checks (before the window starts)

- [ ] **§0 has been deployed and verified.** The cutover and its rollback depend on `pgdata14`.
- [ ] **No robot `ON_TASK`.** Pull live state, not a cached view:
  `curl -s http://localhost:8000/api/v1/robots | python3 -c "import json,sys; [print(r['name'], r['status']['state'], 'online' if r['status']['online'] else 'OFFLINE') for r in json.load(sys.stdin)]"`
  and cross-check for any `RUNNING` mission:
  `curl -s http://localhost:8000/api/v1/missions | python3 -c "import json,sys; print('RUNNING:', [m['name'] for m in json.load(sys.stdin) if m['status']['state']=='RUNNING'])"`
  Both must be clear. Run this check again immediately before §2. Don't rely on a check done
  earlier in the day. Note: the robot state in the DB is the last state the robot reported. A robot
  that went offline mid-order stays `ON_TASK` with `OFFLINE` indefinitely (seen in rehearsal). That
  is stale, not a moving robot; confirm with the robot's owner, then treat it as clear.
- [ ] **Safety dump taken.** This is an extra copy taken before anything is touched. It is *not*
  the dump that gets restored (that one is taken inside the window, in §2):
  ```bash
  docker exec -e PGPASSWORD="$PW" "$PG" pg_dump -Fc -U "$USR" "$DB" \
    > "$CUTOVER_DIR/safety-precheck.dump"
  ls -la "$CUTOVER_DIR/safety-precheck.dump"
  docker exec -i "$PG" pg_restore -l < "$CUTOVER_DIR/safety-precheck.dump" | grep -c "TABLE DATA"
  ```
  Copy it off the host.
- [ ] **Old volume confirmed.** `docker inspect "$PG" --format '{{json .Mounts}}'` shows
  `pgdata14`'s name (`b323568f…0fa52`). Write it here:
  `OLD_VOLUME_ID = _______________________________`
- [ ] **Roles and databases unchanged.** The dump uses `pg_dump` (one database), not
  `pg_dumpall`. The reason: a read-only check on 2026-09-24 found exactly one non-`pg_` role,
  the app role (superuser, login). The databases were `postgres` (no user objects), the app
  database, and the two templates. There are no per-role/per-database settings
  (`pg_db_role_setting` is empty) and no default ACLs. The new image creates the app role and
  the app database from `POSTGRES_USER`/`POSTGRES_DB`, so `pg_dumpall --globals-only` would add
  nothing. Re-confirm on the day. If either query returns anything beyond the above, add
  `pg_dumpall --globals-only` to §2 and restore it before §3.4:
  ```bash
  psqlq -c "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_'" | wc -l      # expect 1
  psqlq -c "SELECT datname FROM pg_database ORDER BY 1"                        # expect 4
  ```
- [ ] **Timezone noted.** Production `SHOW timezone` = `Etc/UTC` (checked on 2026-09-24). The
  only timestamp column in `public` is `mission_trajectory.ts`, which is `timestamptz`. Setting
  `timezone='UTC'` on the new server therefore does not change how any stored timestamp renders.
  Re-check on the day: `psqlq -c "SHOW timezone"`.
- [ ] Confirm the pinned image tag, `timescale/timescaledb-ha:pg17.11-ts2.30.1`, and that it is
  already pulled (`docker image ls timescale/timescaledb-ha`). The rehearsal showed it boots as a
  plain single-node Postgres, with no Patroni bootstrap. `POSTGRES_USER` becomes the real
  superuser/owner, and PGDATA is `/home/postgres/pgdata/data`. Don't move off this pin without a
  specific reason.
- [ ] Create the new data volume ahead of time, so the window doesn't depend on it:
  `docker volume create sati_pgdata17`. It must be **empty**: the image only runs initdb, the
  timescaledb-tune step (§3.1) and the extension setup on an empty data directory. Rehearsal
  confirmed an empty external volume gets the right ownership on first boot.
- [ ] **The memory/CPU sizing values for §3.1 are agreed** with the host owner (`TS_TUNE_MEMORY`,
  `TS_TUNE_NUM_CPUS`). The runbook uses the rehearsed 4GB / 2 CPUs.

`[TIMING: rehearsal 2026-09-24: §1 commands < 1 s; announce/volume/image steps are manual]`
- [ ] Announce the window. Confirm that nobody else is about to run `restart_services.sh` or edit
  `docker_compose/mission_dispatch_services.yaml` at the same time.

## 2. Stop apps, in-window dump and pg14 baseline, stop Postgres

`[TIMING: window start — rehearsal 2026-09-24: T+0]`

Re-run the `ON_TASK` / `RUNNING` check from §1 first.

1. **Stop the Postgres-using services, in this order.** Postgres stays up.
   `agent-orchestrator-service` does not use Postgres: it only gets `POSTGRES_PASSWORD` to satisfy
   `packages/config.py` at import, and it has no database code. Leave it running, together with
   the LiveKit services, ArangoDB, MinIO and Mosquitto.
   ```bash
   $COMPOSE stop mission-dispatch
   $COMPOSE stop api-delegation-service
   $COMPOSE stop graph-builder-service
   $COMPOSE stop mission-planner-service
   ```
   Confirm that no app connections remain:
   `psqlq -c "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"` → `0`.
   `mission-dispatch` does not handle SIGTERM: its `stop` always waits the full 10 s grace and
   ends with exit code 137 (SIGKILL). That is expected and is most of this step's time.
   `[TIMING: apps stopped — rehearsal 2026-09-24: 12 s]`
2. **In-window dump.** This is the one §3 restores. Nothing writes after this point:
   ```bash
   docker exec -e PGPASSWORD="$PW" "$PG" pg_dump -Fc -U "$USR" "$DB" \
     > "$CUTOVER_DIR/cutover-inwindow.dump"
   ls -la "$CUTOVER_DIR/cutover-inwindow.dump"
   sha256sum "$CUTOVER_DIR/cutover-inwindow.dump" > "$CUTOVER_DIR/cutover-inwindow.dump.sha256"
   ```
   `[TIMING: in-window dump complete — rehearsal 2026-09-24: < 1 s (15 KB dump)]`
3. **pg14 catalog baseline.** Save this function once, run it now against pg14, and run it again
   in §4 against pg17:
   ```bash
   snapshot() {   # usage: snapshot <output dir>
     local out=$1; mkdir -p "$out"
     psqlq -c "SELECT table_name, ordinal_position, column_name, data_type, udt_name,
                      is_nullable, column_default, character_maximum_length,
                      numeric_precision, numeric_scale
               FROM information_schema.columns WHERE table_schema = 'public'
               ORDER BY 1, 2" > "$out/columns.txt"
     psqlq -c "SELECT tablename, indexname, indexdef
               FROM pg_indexes WHERE schemaname = 'public'
               ORDER BY 1, 2" > "$out/indexes.txt"
     psqlq -c "SELECT cl.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
               FROM pg_constraint con
               JOIN pg_namespace n ON n.oid = con.connamespace
               LEFT JOIN pg_class cl ON cl.oid = con.conrelid
               WHERE n.nspname = 'public'
               ORDER BY 1, 2, 4" > "$out/constraints.txt"
     psqlq -c "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
               FROM pg_trigger t
               JOIN pg_class c ON c.oid = t.tgrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND NOT t.tgisinternal
               ORDER BY 1, 2" > "$out/triggers.txt"
     # Functions in public that are NOT extension members. This keeps TimescaleDB's own
     # functions out of the diff even if the image pre-created the extension.
     psqlq -c "SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                      pg_get_function_result(p.oid), md5(p.prosrc)
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'public'
                 AND NOT EXISTS (SELECT 1 FROM pg_depend d
                                 WHERE d.classid = 'pg_proc'::regclass
                                   AND d.objid = p.oid AND d.deptype = 'e')
               ORDER BY 1, 2" > "$out/functions.txt"
     # Exact per-table row counts, generated from the catalog and executed with \gexec.
     docker exec -i -e PGPASSWORD="$PW" "$PG" psql -X -At -v ON_ERROR_STOP=1 -U "$USR" -d "$DB" \
       > "$out/counts.txt" <<'SQL'
   SELECT format('SELECT %L, count(*) FROM public.%I', table_name, table_name)
   FROM information_schema.tables
   WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
   ORDER BY table_name
   \gexec
   SQL
   }
   snapshot "$CUTOVER_DIR/baseline-pg14"
   wc -l "$CUTOVER_DIR"/baseline-pg14/*.txt   # none empty except possibly triggers/functions
   ```
   As of 2026-09-24, `public` held 6 tables (`robotobjectv1`, `missionobjectv1`, `mapobjectv1`,
   `settingsobjectv1`, `detectionresultsobjectv1`, `mission_trajectory`).
   In rehearsal `triggers.txt` and `functions.txt` were empty (0 lines); the others were not.
   `[TIMING: baseline captured — rehearsal 2026-09-24: 1 s]`
4. **Stop Postgres:**
   ```bash
   $COMPOSE stop postgres
   ```
   `[TIMING: postgres stopped — rehearsal 2026-09-24: < 1 s]`

## 3. Image swap, config, restore

1. **Edit `docker_compose/mission_dispatch_services.yaml`.** In the `postgres` service:
   ```diff
      postgres:
   -    image: postgres:14.5
   +    image: timescale/timescaledb-ha:pg17.11-ts2.30.1
   +    # v2 WP1.2 settings. Do NOT override shared_preload_libraries here: the image already
   +    # ships 'timescaledb,pg_textsearch', and a -c override replaces the whole list.
   +    command: ["postgres",
   +              "-c", "timescaledb.telemetry_level=off",
   +              "-c", "timezone=UTC"]
        environment:
          - POSTGRES_USER=${POSTGRES_DATABASE_USERNAME}                # these four unchanged
          - POSTGRES_PASSWORD=${POSTGRES_DATABASE_PASSWORD}
          - POSTGRES_DB=${POSTGRES_DATABASE_NAME}
          - POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256
   +      # timescaledb-tune runs once, at initdb, and sizes from the container's cgroup limits.
   +      # Production has no limits, so without these it sizes from the whole shared host
   +      # (187 GB / 24 CPUs => shared_buffers ~47 GB). Pin it to the rehearsed values.
   +      - TS_TUNE_MEMORY=4GB
   +      - TS_TUNE_NUM_CPUS=2
   +      - TS_TUNE_MAX_CONNS=100        # pg14 had 100; tune would otherwise pick 50 at 4GB
   +      - TIMESCALEDB_TELEMETRY=off    # also written to postgresql.conf + telemetry job unscheduled
        volumes:
   -      - pgdata14:/var/lib/postgresql/data
   +      - pgdata17:/home/postgres/pgdata      # PGDATA is /home/postgres/pgdata/data
   ```
   and at the top level (**keep `pgdata14`**, because rollback uses it):
   ```diff
    volumes:
      arango_data:
      pgdata14:
        external: true
        name: b323568ff9d35df1ea2dfd6916624635ca53953379106159029ca3dd0830fa52
   +  pgdata17:
   +    external: true
   +    name: sati_pgdata17
   ```
   The volume must be mounted at the parent `/home/postgres/pgdata`, not at `…/data`. The
   rehearsal used exactly this.
   **Verified in the 2026-09-24 rehearsal:** `command:` with `-c` flags works with the ha image's
   `/docker-entrypoint.sh` (the settings show `source = command line`), and an empty external
   volume gets the right ownership on first boot (`fixing permissions on existing directory
   /home/postgres/pgdata/data ... ok`). The `TS_TUNE_*` values were verified in a separate
   throwaway container with **no** cgroup limit, like production: `shared_buffers=1GB`,
   `effective_cache_size=3GB`, `max_connections=100`. Production uses ~16 client connections.
   Before `up`, check the rendered config:
   `$COMPOSE config | grep -A30 '^  postgres:' | grep -E 'image|TS_TUNE|TIMESCALEDB|pgdata|telemetry|timezone'`
   (don't paste the full output anywhere: it contains the password).
   `[TIMING: compose edit — rehearsal 2026-09-24: a prepared file was swapped in; allow ~2 min to edit by hand]`
2. **Bring up the new Postgres:**
   ```bash
   $COMPOSE up -d postgres
   $COMPOSE ps postgres        # wait for (healthy); healthcheck is pg_isready, unchanged
   docker logs "$PG" 2>&1 | grep -E "Recommendations based on|init process complete|ready to accept"
   ```
   The first boot runs initdb, `001_timescaledb_tune.sh` and the extension scripts, then restarts
   the server. The log must say `Recommendations based on 4.00 GB of available memory and 2 CPUs`.
   If it names the host's full memory, `TS_TUNE_*` did not apply: stop, fix §3.1, remove and
   re-create the **empty** `sati_pgdata17`, and `up` again (tune only runs on an empty volume).
   `[TIMING: new postgres healthy — rehearsal 2026-09-24: 6 s after up]`
3. **Record whether the image pre-created the extension** (this decides what §4.2 expects):
   ```bash
   psqlq -c "SELECT extname, extversion FROM pg_extension ORDER BY 1"
   ```
   Rehearsal: `plpgsql 1.0`, `timescaledb 2.30.1`, `timescaledb_toolkit 1.26.0`. The image's
   `000_install_timescaledb.sh` creates `timescaledb` in `postgres`, `template1` and `$POSTGRES_DB`.
   The restore on top of that is clean.
4. **Restore the in-window dump from §2.2, as the app role.** This makes the app role the owner of
   everything the restore creates, which resolves the pg15+ `public`-schema privilege change. The
   rehearsal confirmed this.
   ```bash
   sha256sum -c "$CUTOVER_DIR/cutover-inwindow.dump.sha256"
   docker cp "$CUTOVER_DIR/cutover-inwindow.dump" "$PG":/tmp/cutover-inwindow.dump
   docker exec -e PGPASSWORD="$PW" "$PG" \
     pg_restore --no-owner --role="$USR" -U "$USR" -d "$DB" --exit-on-error \
     /tmp/cutover-inwindow.dump
   ```
   `[TIMING: restore complete — rehearsal 2026-09-24: < 1 s]`

## 4. Verification, then extension

1. **Server settings (v2 WP1.2):**
   ```bash
   psqlq -c "SHOW server_version"                   # 17.11
   psqlq -c "SHOW shared_preload_libraries"         # must contain timescaledb
   psqlq -c "SHOW timescaledb.telemetry_level"      # off
   psqlq -c "SHOW timezone"                         # UTC
   psqlq -c "SHOW shared_buffers"                   # 1GB   (tune, from TS_TUNE_MEMORY=4GB)
   psqlq -c "SHOW effective_cache_size"             # 3GB
   psqlq -c "SHOW max_worker_processes"             # 21
   psqlq -c "SHOW timescaledb.max_background_workers"   # 16
   psqlq -c "SHOW max_connections"                  # 100  (TS_TUNE_MAX_CONNS)
   ```
   All passed in the 2026-09-24 rehearsal (`server_version` = `17.11 (Ubuntu 17.11-1.pgdg22.04+2)`).
   - `shared_preload_libraries` = `timescaledb,pg_textsearch`. The ha image ships it in
     `/usr/share/postgresql/17/postgresql.conf.sample` (line 773), and initdb copies it into
     `$PGDATA/postgresql.conf`. It is not set by tune or by the entrypoint. Leave it alone.
   - `timescaledb-tune` **does** run at initdb (`/docker-entrypoint-initdb.d/001_timescaledb_tune.sh`,
     disable with `NO_TS_TUNE`). It reads the container's cgroup memory/CPU limits and, when
     there are none, the whole host. §3.1 pins it with `TS_TUNE_*`; tune writes its values into
     `postgresql.conf`, so they persist but can be changed later with `-c` if needed.
   - The image's `000_install_timescaledb.sh` appends `timescaledb.telemetry_level=basic` to
     `postgresql.conf` unless `TIMESCALEDB_TELEMETRY=off` is set. The `-c` flag wins either way
     (it shows `source = command line`); the env var makes the file agree and unschedules the
     telemetry job.
   - If the telemetry setting shows `unrecognized configuration parameter`, timescaledb is not
     preloaded. Something overrode `shared_preload_libraries`; remove that override.
   `[TIMING: settings checks — rehearsal 2026-09-24: < 1 s]`
2. **Catalog and row-count diff. Run this *before* `CREATE EXTENSION`**, as the rehearsal did:
   ```bash
   snapshot "$CUTOVER_DIR/after-restore-pg17"
   diff -r "$CUTOVER_DIR/baseline-pg14" "$CUTOVER_DIR/after-restore-pg17" \
     && echo "CATALOG + COUNTS IDENTICAL"
   ```
   Every file must diff empty. The `functions.txt` query excludes extension members, so even if
   §3.3 showed the image pre-created `timescaledb`, its functions don't show up here. If a diff
   shows only a *textual* difference in `pg_get_*def` output (a deparse format change between
   14 and 17, not a semantic one), stop and have a second person confirm it's cosmetic before
   continuing. The rehearsal saw none.
   `[TIMING: verification complete — rehearsal 2026-09-24: 1 s, CATALOG + COUNTS IDENTICAL]`
3. **Extension.** This is idempotent, because the image may already have created it:
   ```bash
   psqlq -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
   psqlq -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"   # 2.30.1
   ```
   The `CREATE EXTENSION` prints `NOTICE: extension "timescaledb" already exists, skipping`
   (§3.3), which is expected. Then confirm the `public` table set and row counts are unchanged.
   The extension adds objects in its own schemas, plus extension-member functions in `public`.
   It does not touch user tables:
   ```bash
   snapshot "$CUTOVER_DIR/after-extension-pg17"
   diff "$CUTOVER_DIR/baseline-pg14/counts.txt" "$CUTOVER_DIR/after-extension-pg17/counts.txt" \
     && echo "COUNTS IDENTICAL"
   ```
   `[TIMING: extension step — rehearsal 2026-09-24: < 1 s]`

If anything in §4 fails to verify: **stop. Do not start the application services. Go to §7.**

## 5. Start order (reversed)

```bash
$COMPOSE up -d mission-planner-service
$COMPOSE up -d graph-builder-service
$COMPOSE up -d api-delegation-service
$COMPOSE up -d mission-dispatch
```

After each one comes up, confirm clean startup logs before starting the next: Postgres, Arango,
MinIO and MQTT all connect, and there are no tracebacks. This is the same bar as the commit A/B
production verification. A live process is not enough evidence on its own; check the actual
connection log lines. (`up -d api-delegation-service` also ensures its `depends_on` services are
up. They are already running and unchanged, so nothing should be recreated. Check the
`up` output for any unexpected `Recreate`. Those dependencies include `livekit-service`: fine on
production, where it's running, but on the bridge staging stack use
`up -d --no-deps api-delegation-service`, or compose starts a staging `livekit-service` on the host
network next to production's.)

What the clean-startup evidence looks like (rehearsal): planner and graph-builder log
`Connected to ArangoDB`, `Connected to MinIO` and `Successfully connected to MQTT broker`, then
`Application startup complete`. The API logs `Database watchers started`. `mission-dispatch` prints
no explicit Postgres connect line; the evidence is one `Object from DB: <name>` per mission/robot
followed by `[<robot>] Created robot` for each robot. Then check
`docker inspect -f '{{.RestartCount}}'` is `0` for all four.

`[TIMING: all services up — rehearsal 2026-09-24: 18 s (4 services, ~4 s log check each)]`

## 6. End-to-end check

**On staging (rehearsal only, never against production robots):**
1. Run the dummy robot simulator (`tests/dummy_robot/dummy_robot.py`) against the staging broker,
   targeting a registered test robot. Rehearsal commands (the `register_robot.sh` /
   `send_test_mission.sh` helpers are stale: they target port 5000 and old routes):
   ```bash
   API=http://127.0.0.1:18000/api/v1
   curl -s -X POST $API/robots -H 'Content-Type: application/json' -d '{"name":"stg-dummy-01"}'
   docker build -t satinav-staging-dummy-robot:rehearsal -f tests/dummy_robot/Dockerfile .
   docker run -d --rm --name satinav-staging-dummy-robot --network satinav-staging_staging \
     satinav-staging-dummy-robot:rehearsal python tests/dummy_robot/dummy_robot.py \
     --robot_name stg-dummy-01 --mqtt_host mosquitto --mqtt_port 1883 \
     --mqtt_prefix staging/uagv/v2/RobotCompany --no_images
   ```
   The `--mqtt_prefix` must match staging `mission-dispatch`'s `staging/...` prefix, or the robot
   never sees the order.
2. Create a mission through the staging API, and confirm `PENDING -> RUNNING`. This proves
   LISTEN/NOTIFY dispatch on the new Postgres:
   ```bash
   curl -s -X POST $API/missions -H 'Content-Type: application/json' -d '{"name":"post-cutover-check",
     "robot":"stg-dummy-01","mission_tree":[{"name":"root_sequence","parent":"root","sequence":{}},
     {"name":"goto","parent":"root_sequence","route":{"waypoints":[{"x":5.0,"y":5.0,"theta":0.0,"map_id":""}]}}]}'
   curl -s $API/missions/post-cutover-check | python3 -c "import json,sys; print(json.load(sys.stdin)['status']['state'])"
   docker logs satinav-staging-dummy-robot 2>&1 | grep "Order accepted"
   ```
3. Note: the dummy robot currently runs its own patrol loop instead of following the ordered
   waypoints, so it won't reach `COMPLETED`. That's expected until the week-2 goal-following
   mode is added (see `docs/satinav-fleet-agent-phase0-v2.md`). `RUNNING` with a dispatched
   VDA5050 order is enough evidence for this check. It also never acknowledges `cancelOrder`
   (dispatch gives up after 20 resends) and always reports `ON_TASK` while connected, so stop the
   dummy robot before the §1 / §2 `ON_TASK` check, and delete its missions rather than cancelling
   them.

**On production (the actual cutover, after the staging rehearsal above has passed clean):**
1. The same WAIT-only-mission check used to verify commits A/B: create a mission whose only node
   is a `wait` action, targeting a real registered robot. A `wait` is a dispatcher-internal timer,
   and the code confirms it never sends a VDA5050 order, so there is zero physical-robot risk.
   Confirm dispatch picks it up via NOTIFY within seconds. Delete it afterwards.
2. **One real robot, one short `go_to`.** This is the only step that exercises the order-dispatch
   path against real hardware. Confirm with whoever owns that robot beforehand, pick a short,
   safe, already-mapped waypoint, and watch it complete (`RUNNING -> COMPLETED`) before calling
   the cutover done.

`[TIMING: e2e check complete — rehearsal 2026-09-24 (staging): PENDING -> RUNNING in 0.6 s; 22 s including dummy-robot start]`

## 7. Rollback

**Why this works:** after the §2.2 dump, nothing wrote to `pgdata14`. The apps were stopped, then
Postgres was stopped. The cutover never deletes it: it is `external`, so even `down -v` leaves it
alone. Rollback does **not** rely on compose reattaching an orphaned anonymous volume, because
compose will not do that. It mounts `pgdata14` explicitly by name, which is why §0 is a hard
prerequisite.

1. Stop the Postgres-using apps, in the §2.1 order, then `$COMPOSE stop postgres`.
2. Edit `docker_compose/mission_dispatch_services.yaml` back to the §0 state of the `postgres`
   service. That means `image: postgres:14.5`, no `command:`, no `TS_TUNE_*`/`TIMESCALEDB_*`
   env, and `volumes: [pgdata14:/var/lib/postgresql/data]`. The §3.1 edit is not committed
   during the window, so the simplest way is to discard it:
   ```bash
   git diff --stat docker_compose/mission_dispatch_services.yaml   # must show only the §3.1 edit
   git checkout -- docker_compose/mission_dispatch_services.yaml
   ```
   Don't restore from commit 8fe98d5 (the §0 commit) blindly: that also reverts any later,
   unrelated compose changes. Dropping the §3.1 edit also drops the top-level `pgdata17`
   declaration; the volume itself is **not** deleted. Keep it for forensics.
3. `$COMPOSE up -d postgres`, and wait for healthy (`$COMPOSE ps postgres`).
4. Verify it's the old data: `docker inspect "$PG" --format '{{json .Mounts}}'` shows
   `b323568f…0fa52`, `psqlq -c "SHOW server_version"` shows 14.5, and running
   `snapshot "$CUTOVER_DIR/rollback-pg14"` then
   `diff -r "$CUTOVER_DIR/baseline-pg14" "$CUTOVER_DIR/rollback-pg14"` is empty.
5. Start the apps, in the §5 order, with the same log checks. Then re-run the §6 check that
   applies (WAIT-only mission on production) to confirm dispatch works on pg14 again.
   Rehearsal 2026-09-24: mounts showed the old volume, `14.5 (Debian 14.5-2.pgdg110+2)`, the
   snapshot diff was empty, all four apps came back with 0 restarts and no tracebacks, the
   mission created on pg17 was gone (as expected, see 6), and a new mission went
   `PENDING -> RUNNING` in 0.6 s.
   `[TIMING: rollback — rehearsal 2026-09-24: 38 s wall from first stop to apps back (stop apps +
   postgres 12 s, pg14 healthy 6 s, verify < 1 s, apps up 5 s, plus a 15 s pause); budget 5 min
   including the compose revert and log reading]`
6. **The one real caveat:** anything written *after* the cutover (new missions, robot state
   updates, node updates) is lost on rollback, because it only exists in `sati_pgdata17`. That's
   why the decision window is short.
7. If `pgdata14` is somehow unusable, the fallback is to restore
   `$CUTOVER_DIR/cutover-inwindow.dump` into a fresh `postgres:14.5` container on a new named
   volume.

**Rollback decision window: 1 hour from §5 completing.** Within that hour, roll back rather than
debug live if anything looks wrong: verification queries clean but application behavior off, an
error class not seen in rehearsal, or anything that doesn't match what staging showed. After the
window closes, fix forward instead. A rollback after real production writes have landed on pg17
means losing that data.

`[TIMING: window end — rehearsal 2026-09-24: first app stop -> all apps back 118 s wall, of which
~80 s were deliberate investigation pauses; commands alone ~38 s (12 s stop,
~1 s dump + baseline, 6 s new postgres, ~2 s restore + verification, 18 s start). Budget
15 min for production: hand edit of the compose file, careful log reading, and the §6 production
checks (which were not rehearsed and depend on the robot owner).]`

## 8. Retention of rollback artifacts

This is separate from the 1-hour decision window. Following v2 WP1.8, **keep the old volume
(`pgdata14` / `b323568f…0fa52`) and `cutover-inwindow.dump` for 2 weeks** after the cutover.
The safety dump stays with them, off-host. Then remove them deliberately: a named person, on a
named date, runs `docker volume rm b323568ff9d35df1ea2dfd6916624635ca53953379106159029ca3dd0830fa52`
and removes `pgdata14` from the compose file in a reviewed commit. Never do it via a prune. The
~190 orphaned volumes from the standing warning are a separate decision and are not part of this
cleanup.

## 9. Explicitly out of scope for this window

- Alembic (baseline stamp and the `phase0_core` migration) comes in separate, later windows, only
  after this cutover is confirmed stable. **Do not merge Alembic/`phase0_core` before this
  cutover.** Per v2 §5.3 the API entrypoint will run `alembic upgrade head`. Against pg14 without
  TimescaleDB, that fails, and the API does not start.
- `packages/events` is independent. It is safe to merge any time and is unrelated to this window.
- The mechanical test-debt backlog (`sync_db_client`, `list_bags` signature, etc.) is unrelated.
  Do it whenever there's a gap.

## Rehearsal log

**2026-09-24, timed rehearsal of §1–§7 on a fresh staging stack.** Project `satinav-staging`
(bridge network, own mosquitto, `staging/` MQTT prefix, API only on `127.0.0.1:18000`). Staging
Postgres started on `postgres:14.5` with an external named volume (mirroring §0), loaded from a
fresh read-only `pg_dump -Fc` of production (17 missions, 3 robots, 2 maps, 1 settings), restored
as the staging app role. The §3.1 edit was mirrored by a third `-f` override file. The rollback
(§7) was rehearsed for real, back onto the untouched pg14 volume. The staging stack and its
volumes were removed afterwards. Production was only read (`pg_dump`, `SELECT`).

Results: every §4.1 `SHOW` passed; the §4.2 catalog + count diff was empty; `PENDING -> RUNNING`
on pg17 and again after rollback on pg14. No step needed a retry.

Changes made to this runbook:

1. **§3.1: added `TS_TUNE_MEMORY=4GB`, `TS_TUNE_NUM_CPUS=2`, `TS_TUNE_MAX_CONNS=100`,
   `TIMESCALEDB_TELEMETRY=off`.** The ha image runs `timescaledb-tune` at initdb and sizes from
   cgroup limits, falling back to the whole host. Staging only looked sane because of its 4 GB /
   2 CPU limit. Uncapped on this host (187 GB, 24 CPUs) a dry run gives `shared_buffers=47872MB`,
   `effective_cache_size=143616MB`, `max_parallel_workers_per_gather=12`. Also, at 4 GB tune picks
   `max_connections=50` versus pg14's 100, so it is pinned to 100. Verified in a limit-free
   throwaway container. Added a §1 pre-check to agree the sizing, a §3.2 log check that tune used
   4 GB, and the recovery if it didn't.
2. **§4.1: answered the open questions.** `shared_preload_libraries` comes from the image's
   `postgresql.conf.sample` (`timescaledb,pg_textsearch`), so it must not be overridden. Filled in
   the expected values, and added `SHOW max_connections`.
3. **§3.3: recorded that the image pre-creates `timescaledb` 2.30.1 and `timescaledb_toolkit`
   1.26.0**, so §4.3's `CREATE EXTENSION` prints an "already exists" notice.
4. **§4.3: "re-run only counts.txt" had no command.** Added one.
5. **§7.2: "restore the file from the commit that landed §0" was ambiguous and would revert
   unrelated later changes.** It now says to discard the uncommitted §3.1 edit with
   `git checkout --`, after a `git diff --stat` check. Also added a post-rollback dispatch check
   and the rollback timing.
6. **§1: the `ON_TASK` check now prints `online`.** A robot that disconnects mid-order stays
   `ON_TASK` in the DB. Also added a runnable `RUNNING`-mission one-liner in place of the bare
   `curl`.
7. **§5: documented the start-up evidence per service.** `mission-dispatch` has no Postgres
   connect line. Also noted that `up -d api-delegation-service` pulls in the host-network
   `livekit-service` dependency: harmless on production, but staging must use `--no-deps`.
8. **§2.1: noted that `mission-dispatch` ignores SIGTERM**, so its stop always takes 10 s and
   exits 137.
9. **§6 staging: replaced the vague dummy-robot steps with the commands that worked.** The
   `tests/dummy_robot/*.sh` helpers target port 5000 and are stale. Documented that the dummy
   robot must use the `staging/` prefix, never acks `cancelOrder`, and always reports `ON_TASK`.
10. **Shell setup: `chmod 700 "$CUTOVER_DIR"`**, since it holds data dumps and rendered configs
    that contain credentials.

Not runbook bugs, but seen in staging and worth knowing: on a fresh ArangoDB,
`mission-planner-service` crashed once on start (`GraphCreateError 409`, racing graph-builder to
create `topological_map`) and recovered through `restart: on-failure`. The staging API's
`+/diagnostics` MQTT client connects to `localhost` (the staging override doesn't set its MQTT
host), so it logs `Connection refused`. Neither applies to production.
