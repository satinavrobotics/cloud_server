# Scene graph: integration plan

Status: proposal, revised after review. Companion to `design.md` (the algorithm); this file scopes how it joins the
running system.

## Decisions

1. **The leaf graph stays.** Topo nodes and edges (`nodes_{id}`, `edges_{id}`, `GET /maps/{id}/graph`) are unchanged
   and remain the record of what each mission and session laid down (logs: `session_trace`, `edge-stats`, run views).
   The high-level graph is *additional*, derived from the leaves; it is what the map window displays and what
   planning uses.
2. **Planner output keeps its shape**: `/api/v1/plan` returns `waypoints` as poses, as today; dispatcher, order
   building, mapId and frame handling are not touched. Two fields need a defined meaning, see "Planner contract".
3. **The builder is a separate worker inside the graph-builder service**, outside the MQTT/upload path and
   `_upload_lock`, fed from the database, one writer per map (advisory lock).
4. **Costmap row order is verified** (nav2, row 0 = origin y); the library's convention is correct. No open item.

## Corrections from review (facts about the library)

* `Router.cost(a, b)` returns a float only, over coarse node indices; it has no path and no point lookup, and
  `_Region.node()` returns -1 off the lattice (which would index the last node). It is not a planning API.
* The coarse lattice is small (about 1.7k nodes for office8), so a plain Dijkstra on it gives the exact path, takes a
  blocked overlay trivially, and has no portal-sampling stretch (1.00-1.24). **Planning therefore searches the coarse
  lattice directly.** Regions, anchors and links are for display, "go to place" goal picking and hierarchy; `Router`
  is demoted to an optional accelerator for very large maps.
* There is no serialization. State to persist: the `CellMap` chunks (value, owner, visited), `anchors`, `_next`,
  `_last` (per session; without it the first leaf after a restart skips `clear_path`) and the params hash.
* Fold is destructive and the result history-dependent. A leaf delete or a session realign is therefore a **full
  refold** (O(N) costmap fetches from MinIO) and changes partition and anchor ids. This supersedes design.md's
  "a leaf is folded once and not queried again" for those two events only.
* One leaf costs `_rebuild()` plus a `_Region` construction again in `links()` and in `build_levels()`.

## Pieces

| # | Piece | Scope |
|---|---|---|
| 1 | **Library** (first) | `to_snapshot`/`from_snapshot` (chunks, anchors, `_next`, `_last`, params hash; `.npz`); `fold_many(leaves)` (fold all, rebuild once) for catch-up and backfill; one shared `_Region` for `links()`, `build_levels()` and planning; `locate(x, y)` snapping to the nearest navigable node; `route(a, b)` returning the node path on the coarse lattice with an optional blocked overlay; `log` made optional; leaf resampling from a rotated map frame (nearest neighbour, unknown mask preserved) and cell alignment against the payload `resolution`; level ids keyed `(level, id)` with a stable parent id; scipy in requirements |
| 2 | **Ingest** | One mechanism: a per-map monotonic `sg_seq` assigned when a node's costmap record is stored, plus a watermark in `sg_meta`. The worker folds leaves in `(costmap stamp, id)` order past the watermark. `run_id` is stamped at the same write (merged from the former run-to-leaf piece; `session_id` is already stored) |
| 3 | **Storage** | Per-map `sg_anchors_{id}`, `sg_links_{id}`, `sg_meta_{id}` in Arango (dropped by the delete saga with the rest). One immutable `snapshot-{version}.npz` in MinIO `map-{id}/scene_graph/` plus a `latest` pointer; a derived PNG of the owner plane for the client if wanted. No per-anchor rasters (the snapshot's owner plane replaces them). Params live in `sg_meta` with their hash; a params change forces a full rebuild |
| 4 | **Worker** | Snapshot every N leaves or T seconds and at idle; replay is bounded to the delta since the last snapshot. Publish links/levels once per batch or at most every T seconds. Rebuilds from deletes and realigns are debounced. Version rule: anchors and links are written in one transaction and `sg_meta.version` is set last |
| 5 | **Lifecycle** | Map delete: saga drops collections and objects. Node delete: debounced full refold. Realign: verify whether node poses and `origin_map` are rewritten by `_realign_session`; then refold. Backfill script for existing maps (as the edge-stats one). Anchor ids change on a refold: remap by maximum overlap with the previous ids, or accept churn |
| 6 | **Read API** | `GET /maps/{id}/scene-graph?level=&version=` (anchors, links, hierarchy, `version`, ETag); `scene_graph_updated {version}` on `/ws/map/{id}`, so a client never mixes versions; `MAP.SCENE_GRAPH_*` events |
| 7 | **Client** | `SceneGraph` types, `useSceneGraph`, anchor/link/region layers; map window and goal picking use it; `/graph` feeds only per-run log views (`RunStatsTab`, trace). Thumbnails: representative visited leaf per anchor |
| 8 | **Planner** | Behind a flag, and a per-request fallback to the old search when the scene graph is missing, stale (version lag over a threshold) or start/goal not navigable after snapping. Point -> `locate` -> `route` on the coarse lattice -> waypoints from the node path (simplified). Blocked nodes and `edgeBlocked` become a transient overlay (disk of radius about robot radius + margin around the position, the segment for an edge) on the lattice, so blocks inside a region work |
| 9 | **Ops** | Dockerfile/requirements, compose and CLAUDE.md entries, build-latency and version-lag metrics, integration test on real Arango/Postgres, robot radius as per-map setting stored in `sg_meta` |

Dropped: "link cost from observation". Edge observations accrue per leaf edge and routes no longer follow leaf edges,
so the feed would dry up; revisit when the lattice route has its own traversal statistics.

## Planner contract

* `waypoints[i].node_id`: the nearest leaf within a threshold, else `None`. The dispatcher resolves a robot-reported
  blocked node by `waypoints[i].node_id` with a synthetic `@x,y` fallback (`controllers/mission/server.py` blocked
  handling), so blocks that match by position still work when it is `None`.
* `start_node_id` / `end_node_id` / `path` / `path_length`: filled from the snapped leaf pair and the lattice path
  length; the first-node drop logic is kept on the same inputs.

## Order

1 -> 2 + 3 -> 4 -> 6 -> 7. Piece 5 (delete/realign) follows 1 and 4 (it needs snapshot and refold). Piece 8 can be
prototyped offline against the snapshot before UI, and switches on last, per map, after a corridor/door map has
validated the structure (the one real map so far is a single room).

## Open

* Robot radius source: per map (stored in `sg_meta`) for the first production run; per robot later.
* Anchor-id continuity across refolds: remap by overlap versus accept churn.
* Which costmap layer feeds the graph.
