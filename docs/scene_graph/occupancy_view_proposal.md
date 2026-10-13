# Live occupancy view: plan

Status: proposal, not started. Revised 2026-10-13 after review. Single plan for the feature. Height and colour stay batch
(reconstruction, unchanged). Owners: CIMBI (`packages/scene_graph`), LACI (read API, nginx, measurements), DAVE (`sati-client`, §5 and the DAVE table in §6).

Goal: a viewer shows the fused occupancy `V` of a map (0 free, 100 blocked, 255 unknown), in two cases:

- **Live:** while a mapping session is open, the view grows as the robot maps. Each save refreshes only the changed chunks.
- **Pulled-once:** a map that is no longer mapped loads in a fixed small number of requests, then stays cached.

Both use one data path. The live path is the static path plus a diff.

---

## 1. Verified facts

| # | Fact | Where |
|---|------|-------|
| F1 | The scene graph folds per map: `V` u8 (0/100/255), `T` i64 capture time, `B` i64 blocked time, at the costmap cell (0.05 m). Optional channels `c.<name>.v/.t`. | `engine.py` (`_V`, `_T`, `_B`, `_chan`), `_fold_into` |
| F2 | A tile is 128 × 128 costmap cells (6.4 m). A page is 8 × 8 tiles (51.2 m). | `store.py` `TILE_F = 128`, `_tile_range` |
| F3 | A tile object packs every plane of the tile: `v` (16 384 B), `t` (131 072 B), `b` (131 072 B), `own` (64 × 64 int32 = 16 384 B), with a JSON header and zlib. Raw total 288 KiB. | `store.py` `tile_arrays`; `store_codec.py` `pack`, `compress_object` |
| F4 | A tile's name is the hash of all of F3. The hash changes when `t`, `b` or `own` change, even if `v` is identical. | `store_codec.py` `hash_arrays` |
| F5 | A save writes only dirty tiles. Unchanged tile hashes are reused. | `store.py` `save`, `_pack_tiles` (~line 417) |
| F6 | Tile objects are `g{generation}/t/{hash}`. An empty tile is dropped from the manifest. | `store.py` `_pack_tiles` (~line 423) |
| F7 | The manifest expands all pages of a version into a `"tx,ty" → hash` table. The first read of a version pays for the whole table. | `store.py` `_expand`, `_page` |
| F8 | `manifest`, `tiles_in` and `view` need no engine. The API can read tiles without loading the fold. | `store.py` lines 493, 646, 641 |
| F9 | `gc` keeps the last `keep_last` (default 5) versions plus leases. `lease` is in-process only. | `store.py` `gc` (~672), `lease` (~568) |
| F10 | No public accessor returns `v` of one tile. `_tile` is private and returns the full decoded tile. | `store.py` `_tile` (~506) |
| F11 | The `scene_graph_updated` hint carries `{version, bbox, changed, retired, truncated}`. It is pushed after each save. | `services/graph_builder/scene_events.py` |
| F12 | Existing scene-graph reads are guarded by `_require_service()`. The raster route `/scene-graph/raster` serves the owner plane (chart keys, u16), capped at 4 M cells. It does not serve `V`. | `packages/api/main.py` (~604–660); `scene_graph_reads.py` |
| F13 | The client's `OCCUPANCY_RGBA_LUT` has 101 entries, indexed by `flatData × 100`. The u8 encoding (0/100/255) does not index it, and 255 is out of range. | `sati-client/hooks/useCostmapCanvas.ts` |
| F14 | **Build gating already exists in ingest.** Depth and costmap uploads are accepted only from an unpaused, placed MAPPING session. An operate session, a paused session or an unplaced one is rejected. So the fold only ever sees mapping-session data. | `services/graph_builder/ingest.py` `decide`, `costmap_record`; `server.py` ~857 |
| F15 | The scene worker runs for every map whose per-map switch is on. Its only skip is `map_not_enabled`. It does not check sessions, because ingest already filters what it folds. | `scene_host.py` `skip_reason`, `list_maps` |
| F16 | `map_sessions` records the purpose (mapping or operate) and the placed flag per session. | `packages/utils/map_sessions.py` |

### Corrections from the review

- **Tile identity is coarse.** A tile re-hashes when only `t`, `b` or `own` change (F4). That causes refetches with no new
  occupancy. Measured in §8 before any fix (C-2).
- **Decoding a tile for `v` is expensive.** The whole 288 KiB tile is decoded to return 16 KiB (F3, F10). Decoded tiles are
  cached, and the decode runs in a worker thread, never per request.
- **Leases do not cover gc in another process** (F9). A read can lose an object mid-load. The read maps a missing object to
  410 and the client restarts the whole load (§4).
- **The LUT cannot be reused** (F13). A 256-entry LUT is needed, with 255 transparent.
- **The header had a stale-version bug** (fixed): the chunk header carries no version or epoch, so a 304 can reuse a body
  across versions. The ETag is content-only (§4).
- **Build gating needs no new code** (F14, F15). The earlier plan proposed a build gate. It is not needed, and a second gate
  would only diverge from ingest.

---

## 2. Requirements

- **R1** A pulled-once map loads with a constant small number of requests, bounded by chunks, not tiles.
- **R2** A live save changes the client only in the chunks whose content changed. Unchanged chunks cost no bytes and no
  GPU upload.
- **R3** One load is consistent: every chunk of one load comes from one version, or the client restarts at a newer one.
- **R4** The API does not load the fold engine for occupancy.
- **R5** Unknown (255) renders transparent. Free and blocked use the existing palette.
- **R6** Server memory is bounded by a byte budget. An oversized request is refused, not partly served.
- **R7** No new websocket payload. The existing hint is enough.
- **R8** Building is gated by mapping sessions (F14). Serving is not: a pulled-once map has no session.

---

## 3. Data model

### 3.1 Chunk

A chunk is one page: 8 × 8 tiles, 1024 × 1024 cells, 51.2 m square. Chunk `(cx, cy)` covers `tx ∈ [8cx, 8cx+8)` and
`ty ∈ [8cy, 8cy+8)`. Slot `s = (ty − 8cy) × 8 + (tx − 8cx)`, 0–63. A slot is present when the tile exists in the version's
manifest. An absent slot means unknown everywhere in that tile.

A present slot's payload is the tile's `v` plane: 16 384 bytes, row-major, row 0 at the tile's low y.

### 3.2 Chunk digest

`digest(chunk) = sha256(sorted("s:hash" for present slots))[:16]`, in hex. Computed from the manifest, with no decode. The
`s:` prefix makes it position-sensitive, so two tiles with identical content swapped between slots change the digest. Equal
digests mean equal chunk content, so the client can keep its copy.

### 3.3 Chunk body (binary)

```
[u32 little-endian: header length H]
[H bytes: UTF-8 JSON header]
[16 384 × n bytes: v planes of the n present slots, in slot order]

header = {
  "format": 1,
  "cx": 3, "cy": -2,
  "tile_cells": 128,
  "cell_m": 0.05,
  "slots": [[0, "3f9a…", 0], [5, "91c2…", 16384]]   // [slot, tile hash, byte offset in body]
}
```

The header has no version and no epoch on purpose. The body is a pure function of the slot hashes, so it can be reused
across versions. The version comes from the extent.

### 3.4 Extent (JSON)

```
GET .../scene-graph/occupancy/extent?version=V (optional)
{
  "version": 37, "epoch": …, "generation": 1,
  "cell_m": 0.05, "chunk_m": 51.2,
  "chunks": [{"cx": 3, "cy": -2, "digest": "a1…", "tiles": 12}, …]   // non-empty chunks only
}
```

About 50 bytes per non-empty chunk. A 10 km² map is about 4 000 chunks at worst, under 200 KB.

---

## 4. API (LACI)

New module `packages/api/occupancy_reads.py`: `OccupancyReads(store)`, built next to `SceneGraphReads` and sharing its
enable flag. It imports only the store's manifest and tile accessors. No engine import (R4).

Routes in `main.py`, guarded by `_require_service()` like the other scene-graph routes:

| Route | Returns |
|-------|---------|
| `GET /api/v1/maps/{map_id}/scene-graph/occupancy/extent` | JSON (§3.4). `version` optional; default current. |
| `GET /api/v1/maps/{map_id}/scene-graph/occupancy/chunk/{cx}/{cy}?version=V` | Binary (§3.3). `version` required for a multi-chunk load. |

Status codes:

- `200`: body as above. `ETag: "occ.{cx}.{cy}.{digest}"`. With a pinned `version`:
  `Cache-Control: public, max-age=31536000, immutable`. Without: `no-cache`.
- `304`: `If-None-Match` matches. The tag is content-only, so an unchanged chunk stays 304 across versions.
- `400`: malformed coordinates or version.
- `404`: unknown map, or the version never existed.
- `410 {"detail": {"error": "version_gone", "current_version": N}}`: a pinned version was collected (F9). The client
  restarts the whole load at `current_version`. Mixing chunks from two versions is not allowed.
- `413`: extent over 20 000 chunks.
- `503 {"error": "scene_graph_unavailable"}`, as elsewhere.

Server internals:

- `chunk(map, version, cx, cy)`:
  1. `man = store.manifest(map, version)`. Collect the present slots.
  2. For each present slot, `store.tile_v_plane(map, version, tx, ty)` (CIMBI C-1), in a worker thread (`asyncio.to_thread`).
  3. Build the body. Cache by `(map, epoch, version, cx, cy)` in an LRU with a byte budget (default 64 MiB), separate from
     the store's tile cache so views and occupancy do not evict each other.
  4. `FileNotFoundError` from a blob read → 410.
- `extent(map, version)`: from the manifest only. Digests in one pass. Cached per `(map, epoch, version)`.
- Single-flight per `(map, version, cx, cy)`, the same pattern as the store's `_tile`.

Serving is not gated by sessions (R8). Whether it follows the per-map switch (F15) is an open question (§11.3).

---

## 5. Client (sati-client, DAVE)

The client consumes the contract in §3 and §4 and does not change it. Any contract change goes through §6.1 first.

### 5.1 Modules

- `services/occupancyApi.ts`: `fetchExtent(mapId, version?)`, `fetchChunk(mapId, cx, cy, version, etag?)`. Typed errors for
  410 and 304.
- `utils/occupancyChunk.ts` (pure, unit-tested to the 95% `utils/` threshold): `parseChunk(buf)`, `chunkDigests`,
  `diffExtent(prev, next) → {changed, removed}`.
- `hooks/useOccupancyLayer.ts`: state `Map<"cx,cy", {digest, bytes, handle}>` and the load's version.
  - On mount, or on a version change: fetch the extent, then the chunks in view (or all, for pulled-once), six in flight.
  - On `scene_graph_updated` for this map (live only): fetch the extent, diff digests, fetch only changed chunks at the new
    version. Unchanged chunks keep their bytes and handles. No request is made for them.
  - On 410: restart the load at `current_version`, and drop the old handles.
  - Loads any map with stored occupancy, whether or not a session is open. Live refresh applies only while a mapping
    session is open, since hints stop when saves stop.
- `components/occupancy/OccupancyLayer.ts`: a custom deck.gl layer (§5.3), added to `DeckGLMap` and `DeckGL3DMap` beside the
  existing layers.

### 5.2 Pulled-once flow

1. `extent` → chunk list.
2. Fetch every chunk, six in flight. A 16-chunk map is 17 requests in total.
3. Each chunk is decoded once and uploaded once as an R8 texture.
4. Pinned chunk URLs are immutable, so a reload is served from the HTTP cache with no request.

Optional, later: persist chunk bytes in IndexedDB keyed by `(mapId, epoch, cx, cy, digest)`. This is a per-viewer cache,
dropped at any time. The server is the source of truth.

### 5.3 Rendering

- One R8 texture per chunk (1024 × 1024, 1 MiB on the GPU). Absent slots are zero-filled with 255 (unknown), so the shader
  needs no special case.
- A 256-entry RGBA LUT texture (1 × 256). Index 255 has alpha 0. Index 0 is free and 100 is blocked, using the existing
  ramp from `OCCUPANCY_RGBA_LUT`. The 101-entry table is not reused (F13).
- The fragment shader samples the R8 texture, looks up the LUT and uses nearest sampling. No CPU colouring, no canvas.
- A live update re-uploads only changed chunks (`texSubImage2D` on the whole chunk, 1 MiB).
- The layer exposes chunk bounds, so deck.gl culls chunks outside the viewport.

Check first: the deck.gl version in `sati-client` supports a custom layer with R8 textures. If not, the fallback is one
`BitmapLayer` per chunk with CPU-coloured canvas (4 MiB per chunk). That is acceptable for pulled-once maps.

### 5.4 Hint handling

The hook reads the same `scene_graph_updated` message the chart hook uses. The two consumers share no state. The occupancy
hook ignores hints for maps it has not loaded.

---

## 6. Ownership

### CIMBI (`packages/scene_graph`)

| Item | What | Acceptance |
|------|------|-----------|
| C-1 | Public `SceneGraphStore.tile_v_plane(map_id, version, tx, ty) -> np.ndarray \| None`. Wraps `_tile`, returns a read-only `v`, or `None` when absent. No format change. | Fake blobs: absent → `None`; present → 128 × 128 uint8 equal to the saved `V`. Thread-safe under concurrent reads. |
| C-2 | **Deferred, gated by §8.** A `v`-only object `g{gen}/o/{hash}` per dirty tile, with the manifest gaining `occ`. | Only if the §8 ratio gate is met. Needs a format bump and a migration note. |
| C-3 | Cell convention, agreed with CIMBI 2026-10-13: costmap cell `k` spans the half-open interval `[k × 0.05, (k+1) × 0.05)` m on x; its centre is `(k + 0.5) × 0.05` m; a point `x` belongs to cell `floor(x / 0.05)`. Tile `tx` covers columns `[tx×128, (tx+1)×128)`, i.e. `x ∈ [tx×6.4, (tx+1)×6.4)` m. The window origin is an exact multiple of 0.05 m, so the engine's `round(x0 / RES)` agrees with `floor` there. The planner lattice indexes with `floor(x / RES) // 2`. | Tests pin the lower-edge convention and the centre offset in both codebases. |
| C-4 | No change to the engine fold, partition, or save semantics. | Existing scene-graph suite passes unchanged. |

CIMBI does not touch the read API, the client, or the hint format. C-1 is in CIMBI's files, so it is requested through CIMBI
and committed by CIMBI, or with CIMBI's agreement, since CIMBI's work is uncommitted in the same worktree.

### LACI

| Item | What | Phase |
|------|------|-------|
| L-1 | `packages/api/occupancy_reads.py`: extent, chunk pack, digests, caches, error mapping (§4). Fake-store tests: pack/unpack round trip; slot order; absent = unknown; digest stable for identical versions; digest changes when one tile changes; 410 on a missing blob. | P1 |
| L-2 | Routes in `main.py`. Route tests like `test_scene_graph_reads.py`: ETag, 304 across versions, 410, 400, 413. | P1 |
| L-3 | Measurement tool `tools/scene_graph_sim/measure_occupancy.py` (§8). Does not exist yet; build it. Reads the store only. | P0 |
| L-4 | nginx: gzip is off today. Enable `gzip on` with `application/json application/octet-stream` in the live http block (confirm which nginx file is mounted). | P1 |

### DAVE (sati-client)

| Item | What | Phase |
|------|------|-------|
| D-1 | R8 smoke test: one 1024 × 1024 `r8unorm` texture in `DeckGLMap` and `DeckGL3DMap`. Entry gate for D-2. (§5.3) | P2 entry |
| D-2 | Client modules, LUT, layer (§5.1–5.3), tests (utils 95%, services 85%). | P2 |
| D-3 | Hint handling (§5.4), and the live refresh gating (§5.1). | P2 |
| D-4 | IndexedDB chunk cache (§5.2). | P3, optional |

### 6.1 Handoffs and interface freeze

- **CIMBI → LACI:** `SceneGraphStore.tile_v_plane` (C-1). LACI's route code waits for it.
- **LACI → DAVE:** the routes, status codes, ETag rule and body format in §3–§4. Frozen at the end of P1. The client's fake
  fixtures are built from §3.3 and §3.4 examples, so DAVE can start before the server is live.
- **Shared:** the `scene_graph_updated` hint (F11), unchanged. DAVE reads it; nobody writes it for this feature.
- A change to §3–§4 after the freeze needs all three owners to agree, and this doc is updated first.

---

## 7. Efficiency

- **Requests.** A pulled-once map costs `1 + N_chunks` requests, `N_chunks` being the non-empty chunks. The tile-per-request
  design it replaces cost `N_tiles`, 64× more for a fully mapped chunk.
- **Bytes.** A chunk is at most 1 MiB raw, and is gzip-compressed on the wire if nginx does it (L-5). Tile decode cost is
  server-side, cached and never reaches the wire.
- **Live.** A save touches a few tiles. The hint gives the bbox, the extent gives digests, and only chunks whose digest
  changed are fetched. Unchanged chunks cost no request.
- **Client CPU.** One upload per changed chunk. No per-cell JavaScript, no canvas per tile.
- **Server CPU.** Each chunk is built at most once per version and cached. The fold is never loaded for occupancy.
- **Build cost.** Zero added. The fold already runs; this adds read paths only.
- **Avoided.** JSON-encoded u8 arrays (about 5× bloat), server-side PNG encoding, and pixels over the websocket.

---

## 8. Measurements and the C-2 gate

Run on at least three real maps (the 5–16-chart maps from the handoff) and one large synthetic map:

- Non-empty tiles and chunks per map.
- Compressed bytes per chunk body (gzip -6, the store's level), and the raw/compressed ratio.
- Cold chunk build latency (decode and pack), and warm latency.
- Over 20 consecutive live saves: chunks whose **digest** changed, versus chunks whose **`v`** changed. The ratio is the cost
  of the coarse identity.
- Extent JSON size for the largest map.

Status: the tool does not exist yet, so none of these numbers are measured. Synthetic 128×128 planes (not real maps) give
about 40–90 B compressed for empty or room-like tiles and about 3.9 KB for noisy ones. Do not use them for the C-2 gate.

Decision rule: C-2 only if digest-changed is more than 3× v-changed **and** the extra write per dirty tile is acceptable.
Otherwise the coarse identity stays. Phase 1 ships without C-2 either way.

---

## 9. Tests and acceptance

1. **Unit (CIMBI):** C-1 tests; the existing scene-graph suite passes unchanged.
2. **Unit (LACI server):** L-1, including digest stability and the 410 path.
3. **Integration** against a real `SceneGraphStore` with the test blob store: save v1; save v2 touching one tile. Check that
   the extent digests differ in exactly the chunk containing that tile, that other chunks' digests and bodies are identical,
   and that v1's chunk body is still readable while pinned.
4. **Route tests:** ETag/304 across versions for an unchanged chunk; 410 after gc in another store instance.
5. **Client unit (DAVE):** parse and diff utilities; LUT maps 255 to alpha 0; the layer re-uploads only changed chunks (fake
   texture handle).
6. **Manual (all three):** a pulled-once map renders in at most `1 + N` requests (network panel). A live map grows while the robot maps,
   and each save shows one extent and only changed chunks.

---

## 10. Phasing

0. **P0, checks and measurements.** L-3, L-5. Done: deck.gl 9.1.14 supports `r8unorm` at the luma level (browser smoke test
   still open); gzip is off in nginx; the measurement tool must be built. No product code.
1. **P1, server.** C-1 (CIMBI), L-1, L-2, §9 tests.
2. **P2, client (DAVE).** D-1, D-2, D-3, with the map-level toggle off by default until the manual check passes on a real map.
3. **P3, optional.** C-2 if §8 says so (CIMBI); D-4 IndexedDB cache (DAVE).

---

## 11. Open questions and decisions

1. **C-1 with CIMBI.** Needs your go-ahead before CIMBI is contacted. Recommendation: yes, it is a single read-only accessor.
2. **Pulled-once scope.** Load the whole map on open, or the viewport and the rest on pan? Recommendation: whole map, since §8
   should show it is small. The viewport filter in §5.1 supports both.
3. **Serving for disabled maps.** Should reads follow the per-map switch (F15), or serve stored data regardless? Recommendation:
   follow the switch, so a switched-off map disappears from the viewer like it does from the scene worker.
4. **Auth.** Confirm `_require_service()` is the right guard, and whether map-level permissions apply on top. None of the
   existing scene-graph routes adds more, as far as checked.
5. **World convention.** Resolved by CIMBI (C-3): `x = col × 0.05 m` is the lower edge of column `col`, and the centre is `(col + 0.5) × 0.05 m`. The tests pin both.
6. **deck.gl version.** Decides between the custom R8 layer and the bitmap fallback (§5.3). Pinned 9.1.14 supports R8 at the
   luma level; the one-tile browser smoke test (D-1) is a P2 entry gate.
7. **Interface freeze (all three).** Agree that §3–§4 are frozen at the end of P1, before DAVE's P2 starts.
8. **§5.1 vs §11.3 (DAVE + LACI).** §5.1 says the hook loads any map with stored data. If §11.3 is decided as "follow the
   switch", §5.1 and the hook change with it. Decide before P2.
