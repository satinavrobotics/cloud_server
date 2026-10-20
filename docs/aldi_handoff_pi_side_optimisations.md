# Aldi handoff: Pi-side actions before the next offline run

Context: the 2026-10-20 aldi session on robot `formidable-peacock` (Pi 4, 3.7 GiB) OOM-looped the graph builder
(incident doc https://claude.ai/artifact/NqHzmGPuu4nyZjv82ADs4f). Server-side fixes are on main (exact engine
optimisations 9084b90, exact incremental fold `perf/inc-combined`, memory guard / cache knobs branches). This file lists
what has to be done ON THE PI; none of it can be done from the server.

## Before the run (must)

1. **Graph-builder memory limit actually 896m.** On the Pi: `grep MEM_ ~/cloud_server/docker_compose/.env.robot`.
   A line `MEM_GRAPH_BUILDER=384m` overrides the new 896m default: delete it. Verify after deploy:
   `docker inspect -f '{{.HostConfig.Memory}}' <graph-builder container>` = 939524096.
2. **Deploy the new images**: `scripts/deploy_robot.sh <tag>` (tag printed by `scripts/build_push.sh` on the server).
   It rolls back by itself if the stack is not healthy. The Pi must reach the registry 100.85.3.47:5050 (Tailscale).
3. **Power**: `vcgencmd get_throttled` must be `0x0`. The incident showed `0x50005` (under-voltage now and since boot):
   use the official 5.1 V / 3 A supply and a short cable. Under-voltage throttles the CPU at random moments.
4. **Reconstruction off** on the Pi (`RECON_MEM_LIMIT` 768m would push the limit sum to ~4.06 GiB > 3.7 GiB RAM).
   Without it the limits sum to ~3.3 GiB.
5. **Route inflation** (planned routes keep away from objects; set on the server 2026-10-21): add to `.env.robot`
   `SCENE_GRAPH_INFLATION_RADIUS_M=0.8`, `SCENE_GRAPH_INFLATION_SCALING=6`, `SCENE_GRAPH_SAFETY_KAPPA=1`, then deploy
   (the planner reads them at start). Without them the Pi's planner uses the old hinge (no buffer beyond the robot's
   circumscribed radius).
6. **Free RAM / swap**: `free -m` before starting; stop anything not in the stack.

## Diagnose the last run (should, 5 minutes)

6. **Why was the aldi_ut engine 262 MB?** The engine keeps dense planes over the bounding box of all costmap windows
   (~38 B per 5 cm cell); 262 MB = ~6.9 M cells = ~130 x 130 m, far larger than an Aldi store. Check for a jumped /
   outlier pose (a second session with another placement, a relocalization jump): in ArangoDB (Pi),
   `FOR n IN nodes_aldi_ut COLLECT s = n.session_id AGGREGATE x0=MIN(n.pose.x), x1=MAX(n.pose.x), y0=MIN(n.pose.y), y1=MAX(n.pose.y), c=COUNT(1) RETURN {s, x0, x1, y0, y1, c}`
   (adjust the collection name). A few far-away poses or two clusters = outlier; tell the server team (a canvas cap
   or a tiled canvas is the fix, decided there).
7. **Logs to collect** for the server team: `docker logs` of graph-builder, api-delegation, arangodb, minio (since the
   run start), `journalctl -k | grep -i oom`, `docker stats --no-stream`, `vcgencmd get_throttled`, `free -m`, `df -h`.

## During the run (watch)

8. `curl -s localhost:8004/stats` every few minutes: process RSS, cgroup memory.current/max, oom_kill count, engine
   nbytes and canvas H x W (new fields). Engine nbytes should stay ~50 MB for a 60 x 40 m store; if the canvas H x W
   jumps suddenly, a pose jumped — note the time.
9. The graph builder now pauses intake above 80 % of its cgroup limit (nothing is dropped; the robot spool holds the
   backlog) and will not re-load a map that killed it during load within 10 minutes. A WARNING / ERROR line says so.
10. Finish only when the backlog is 0 (`/ingest/backlog/formidable-peacock`).

## Later (hardware / setup)

11. **ArangoDB and MinIO on an SSD** (USB 3), not the SD card: the incident had a RocksDB write stall and 5-13 s
    settings syncs, which also caused ArangoDB lock timeouts.
12. Keep the Pi ventilated (38 C was fine) and on wired power during long runs.
