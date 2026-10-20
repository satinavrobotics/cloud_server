# Pi-like end-to-end bench of the scene-graph path

Measures what graph-builder does on the robot's Pi: an ingested node -> `SceneRunner` -> `SceneWorker` (prefetch, PNG decode,
`scene_adapter`, batching, `engine.fold_many`) -> `SceneGraphStore.save`, then a cold restart from the store. Judge an engine
branch with the same command on the branch's worktree and on a baseline worktree.

What is real: `scene_runner.SceneRunner` (pooled, 2 threads, eviction budget), `scene_worker.SceneWorker`, the PNG prefetch pool,
`scene_host.worker_config` / `runner_config` (so every `SCENE_GRAPH_*` setting is the production reader), `ChartGraph`,
`SceneGraphStore` (real save/load/manifests/segments). The environment is taken from `graph-builder-service` in
`docker_compose/mission_dispatch_services_robot.yaml` of the repo under test (step budget 5 s, `BATCH_MAX_LEAVES` 8,
`PREFETCH_QUEUE` 16, `MAX_RESIDENT_BYTES` 120 MB, 2 worker threads; everything else = `packages/config.py` defaults, e.g.
`BATCH_MIN_LEAVES` 6, publish interval 5 s, lazy load, near range 1.3 m from `SceneGraphParamsV1()` defaults). Shell variables
override them. What is faked: the node source (in memory), the PNG fetch (bytes in memory), the Postgres guard (always owner),
and the store backend (a directory, instead of ArangoDB + MinIO, so a restart is a new process reading it back).

Not in the number: MQTT, the upload buffer, ingest of the robot topics, the FastAPI app, Postgres/Arango/MinIO clients. `idle`
reports what the graph-builder imports cost (the rest is a cap, not a measured cost).

## Run

    tools/pi_bench/run.sh <repo-worktree> <out-dir> [rate_s=1.0] [factor=1] [bench.py args...]

Runs two containers (`docker run --rm --cpus=1 --memory=896m --memory-swap=896m`, image `sati-unit-tests:py310-pinned`, repo
mounted read-only, host network not used, nothing running is touched): `ingest` (800 nodes) then `restart` (new process, same
store, 8 more nodes). Exit status 137 = OOM-killed (reported). Results: `<out>/ingest_r<rate>_f<factor>.json` (+ `.samples.jsonl`,
one line per 10 simulated seconds: arrived, handled, backlog, buffered, folds, solves, version, engine MB, RSS),
`restart_r<rate>_f<factor>.json`. Idle RSS:

    docker run --rm -v <repo>:/repo:ro -v $PWD/tools/pi_bench:/bench:ro -v <out>:/work sati-unit-tests:py310-pinned \
        python3 -I /bench/bench.py idle --repo /repo --out /work/idle.json

Options: `--nodes 800`, `--rate` (s between nodes), `--factor` (CPU slowdown, below), `--world-x/--world-y` (60 x 40 m),
`--clutter 1` (pallets/carts and ragged shelf faces), `--profile` (cProfile the `fold_many` calls: where the engine time goes; slows them
~1.5x), `--drain-cap-s`, `--max-real-s`. `ONLY=restart tools/pi_bench/run.sh ...` re-runs just the cold restart on the store an earlier
run left in the out dir. The container runs as your uid so the out dir stays removable. Timings share the host: run baseline and
candidate back to back and repeat; differences under ~30% are noise.

## Simulated time and the Pi estimate

The worker, runner and source see `virtual = skipped_idle + real_elapsed * factor`. Idle waits (the worker's idle sleep) are jumped
over, not slept, so a run takes about as long as the compute. `factor=1` is this machine's speed (x86 numbers); `factor=6` makes
every second of compute count as six simulated seconds against the arrival rate: an ESTIMATE of a Pi (about 6x slower per core
than the build host; calibrate the factor with a Pi measurement when one exists). It scales time only: memory, node count and
fold work are the same, and the reported per-fold seconds are x86 seconds (multiply by the factor for the Pi estimate).

## Synthetic world

A 60 x 40 m supermarket: wall ring, 10 shelf rows (aisles 2.4 m), a boustrophedon survey of 800 nodes (spacing ~0.7 m), 10 x 10 m
occupancy windows at 5 cm (PNG, 200 x 200) with speckle noise, transient "people" blobs on some captures, two sessions. It is
cleaner than a real store (no clutter), so folds are cheaper than on the robot: use the numbers to compare engines and to find the
memory and scaling shape, not as an absolute prediction.

## Reading the result

* `peak_backlog_nodes`, `backlog_at_last_arrival`, `drain_after_last_arrival_s`, `caught_up`: does it keep up. (With
  `BATCH_MIN_LEAVES` 6 a backlog of up to ~6 is normal.)
* `folds`, `solves`, `fold_s_*`: the engine. `fold_s_last10_mean` vs `first10` is the growth with map size. `split_s`: worker
  thread time in fold / save / other (PNG decode, adapt, scan).
* `rss_peak_mb` (VmHWM of the process, includes the bench's own ~80 MB of Python + generated PNGs at start: see `rss_start_mb`),
  `cgroup_peak_mb` (container, includes page cache of the store directory), `cgroup_oom_kills`.
* restart: `engine_load_calls` (store.load seconds), `engine_loaded_at_s`, `total_s`, `rss_peak_mb`.

## Baselines (x86, 2026-10-20, this machine under load ~10 of 24 cores; 800 nodes, 60 x 40 m, clean world)

| | old f05f4a3 | main 9084b90 |
|---|---|---|
| rate 1.0 s/node, factor 1: folds / sum of fold s / mean / last-10 mean | 132 / 35.4 / 0.27 / 0.38 | 133 / 11.6 / 0.09 / 0.12 |
| same, save s / worker-thread total s | 7.8 / 45.5 | 4.4 / 17.7 |
| rate 0.3, factor 1: fold sum s | 22.0 | 11.8 |
| engine MB at 800 nodes (`nbytes`) | 74 | 50 |
| ingest peak RSS (VmHWM) / container peak | 277 / 263 MB | 254 / 240 MB |
| cold restart: store.load s / total s / peak RSS | 1.4 / 2.0 / 246 MB | 0.5 / 0.7 / 198 MB |

Both keep up at 1.0 and 0.3 s/node (peak backlog 7-17 nodes = the batch minimum of 6 plus an idle wake-up; drained within 10 s of
the last node; no OOM). Where main's fold time goes (`--profile`, shares of fold_many): `_solve` 90% = partition 45%
(merge_tree 18%, subdivide 14%), `_evaluate` 34% (`_links` 21%), `_build_F` 11%; leaf folding itself ~3%. Outside the engine: save
~25% of worker-thread time, PNG decode + adapt + bookkeeping ~10%. Per-fold time grows with the map (first 10 folds 0.04 s,
last 10 0.12 s at 800 nodes) because every fold is a full solve.
