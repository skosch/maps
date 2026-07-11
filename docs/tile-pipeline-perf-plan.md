# Tile pipeline performance plan (panning: network + CPU)

**Status:** analysis complete, no fixes implemented. This document is the deliverable of a
profiling/analysis pass on the tile-fetching / caching / mosaic-stitching pipeline, prompted by:
*"panning across the map quickly can be pretty slow, network- and CPU-wise."* It is written to be
self-contained for a future implementation agent. Companion context:
`docs/texture-shading-plan.md` (esp. "Phase 4 integration findings" and all addenda).

Branch: `texture-shading-phase4-integration` (tangram-es submodule on the same-named branch).

---

## 1. Methodology

All measurements were made with temporary instrumentation (since reverted, nothing committed):

- `TANGRAM_TRACING` enabled in `tangram-es/core/include/tangram/log.h` (built-in `LOGT`/`LOGTO`
  in/out timestamps for tile-worker `process()`, URL requests, DB queries).
- Timing/count log lines added to `rasterSource.cpp` (`stitchElevationMosaic`, `computeRugosity`,
  `patchNeighborMosaics`), `gl/texture.cpp` (`Texture::upload`, ≥256px textures),
  `util/builders.cpp` (`buildPolygonGrid` per call), `mbtilesDataSource.cpp`
  (`getTileData`/`storeTileData`).
- Headless runs: Xvfb + `LIBGL_ALWAYS_SOFTWARE=1` (llvmpipe), 1400x1000 window,
  `stylus-bike-hike` style, 3D terrain on, Vancouver area (z≈11.4, lng -123.216 lat 49.435), warm
  sqlite tile caches (`elevation.mbtiles` 183 MB, `stylus-osm.mbtiles` 35 MB). Each session:
  35 s settle, then 6 mouse-drag pans (~400 px each, ~2.4 kpx ≈ 9 tile-widths eastward), then
  25 s settle. Sessions:
  - **A** `pan_warm` (debug): texture shading ON, cold process / warm disk (some network fetches).
  - **E** `rel2_ts_on` (release `-O2`): texture shading ON, everything warm (0 network requests).
  - **F** `rel2_ts_off` (release): texture shading OFF (no mosaics/prefetch), 3D terrain still ON.
    (This session's pan input partially missed — 30 tiles built instead of 65 — so use its
    per-tile stats, not totals.)
  - **G** `rel2_2d` (release): 3D terrain OFF — isolates the terrain-grid tessellation.
  - **H** `rel2_z14` (release): shading ON, z≈14.3 downtown Vancouver — heavy vector tiles,
    partially fresh area (142 network requests).

**Measurement hazard found the hard way:** chained sessions initially produced 3–6x inflated
numbers because the run script's app shutdown was unreliable and a leftover instance of the
previous session's app kept spinning through later sessions. All numbers below are from runs with
a stray-process check + hard kill between sessions; an intermediate set of contaminated debug
comparisons was discarded (see §2.5).

Caveats to keep in mind when reading numbers:

- Debug (`-O0`) numbers are inflated **~20–25x** vs release for tile building (measured directly:
  OSM build avg 281 ms debug vs 12.4 ms release for the same scenario) — never profile tile costs
  on a debug build. Counts (fetches, stitches, uploads, patches, triangles) are deterministic and
  build-independent.
- llvmpipe does GL uploads + mipmap generation in software on CPU threads. Real GPUs make the
  per-upload cost smaller, but the **counts and byte volumes** (the re-upload amplification) are
  platform-independent facts, and mobile GLES drivers are far more sensitive to full-texture
  re-uploads + `glGenerateMipmap` than desktop GL.
- The OSM vector source in this setup is served from the local mbtiles cache; on a network vector
  source the fetch-side findings apply to it too.

Config defaults that matter: `tangram.num_tile_workers` = **2** (`app/src/mapsapp.cpp:807`),
`UrlClient maxActiveTasks` = **20** (`tangram-es/platforms/common/urlClient.h:24`),
in-memory raw-tile cache 16 MB/source (`sceneOptions.h`), built-tile cache limit 512 MB
(`tile_cache_limit`, `mapsapp.cpp:813`), mbtiles cache in default rollback-journal mode
(WAL deliberately not enabled, `mbtilesDataSource.cpp:412`).

---

## 2. What actually happens per newly-visible tile while panning

### 2.1 OSM vector tile (source `osm`, max_zoom 14, avg ~31 KB gzipped in cache)

1. **Fetch chain** (`loadTiles()` → `MemoryCacheDataSource` → `MBTilesDataSource` →
   `NetworkDataSource`): memory-cache miss → DB read on the source's single AsyncWorker thread
   (measured 0.3–4.6 ms avg across sessions, p90 ≤11 ms; includes a synchronous
   `REPLACE INTO tile_last_access` write *per read*) → network only if missing/stale
   (curl p50 80 ms, avg 208 ms, 20 concurrent).
   New downloads are gzip-inflated and stored back (`storeTileData`, own transaction + fsync,
   ~31 KB each).
2. **Tile build on a worker (the big one):** MVT parse, style filtering/JS, geometry building.
   Release, 3D terrain ON: **avg 12.4 ms / p50 8.1 / max 84 ms per tile at z11; avg 47 ms /
   p50 22 / p90 102 / max 357 ms at z14 downtown** (buildings + dense landuse). Of this,
   **`buildPolygonGrid` — the 64×64 terrain-drape tessellation of landuse polygons — is 26–49%
   of total vector-build CPU** (49% at z11: 392 of 805 ms; 26% at z14: 650 of 2466 ms; 55–65% in
   debug sessions where its -O0 penalty is largest). It amplified 177 k earcut triangles into
   1.84 M output triangles (10.4x) in one z11 session; the worst single polygon took 33.8 ms
   (release) / 907 ms (debug).
   Without 3D terrain (session G) the same z11 tiles build at **avg 4.2 ms, p50 1.8 ms** — i.e.
   **3D terrain triples-to-quadruples vector tile build cost**, and grid tessellation is about
   half of the 3D cost.
3. **Elevation raster subtask** attached per vector tile (`RasterSource::addRasterTask`) — usually
   satisfied from the texture cache (dedup with the elevation tile set works).
4. Main thread: task completion, mesh/label finalization, VBO uploads.

### 2.2 Elevation raster tile (source `elevation`, ArcGIS LERC, 257×257 float, avg ~62 KB)

Per **visible** elevation tile with texture shading ON:

1. Fetch: DB read ~0.2–1.4 ms avg or network ~80 ms; LERC decode in `process()` on a worker
   (0.6–2.3 ms avg release, up to ~43 ms for large-relief tiles).
2. **Mosaic stitch on the main thread** (`RasterTileTask::addRaster()` →
   `RasterSource::buildElevationMosaic` → `stitchElevationMosaic`): allocate 768×768×4 B
   (2.36 MB), 9 cell copies (or mirror fills), plus `computeRugosity`. Measured **1.3–2.3 ms avg
   per stitch release (max 5 ms; 2.8–7.8 ms avg debug), 52–63 stitches per session ≈ 80–120 ms
   of main-thread time**, arriving in bursts of several per frame. `computeRugosity` itself is
   0.04 ms — negligible. At stitch time the **median mosaic had only 4–5 of 8 real neighbors**
   (one had 0); the rest were mirror-extrapolated and patched later — which is what drives (5).
3. **GL upload + full mip-chain generation** on the render thread when first bound:
   2.36 MB `glTexImage2D` + `glGenerateMipmap`. Measured 5–10 ms avg per upload (llvmpipe does
   both in software; a real desktop GPU is cheaper on the CPU side, mobile GLES likely comparable
   or worse for the mip regen).
4. **8 neighbor prefetches** (`TileManager::enqueueNeighborPrefetch`): deduped against
   visible tiles, in-flight entries, and the texture cache, so steady-state amplification is the
   one-tile ring around the viewport. Measured: 105 elevation fetches for ~60 visible-tile needs
   at z11, 94 for 52 at z14 (**+75–80% fetch volume**), 83–84 prefetch tasks enqueued per
   session. Prefetch-only tiles skip mosaic building (`isProxy()` guard) but still occupy fetch +
   decode slots on the 2 workers.
5. **Patch storm** (`RasterSource::cacheTexture` → `patchNeighborMosaics`): each arriving tile
   (incl. every prefetch) patches its cell into up to 8 live mosaics; each patch calls
   `Texture::resize()` which flags a **full 2.36 MB re-upload + complete mip regeneration** on
   next bind. Measured per session: 92–99 patch events, 223–260 cell patches, and
   **1.7–2.2x upload amplification** — release z11: 104 uploads for 62 distinct mosaics
   (histogram: 1 upload ×23, 2 ×36, 3 ×3); release z14: 103 for 52; debug: up to 4 uploads for
   one mosaic. That is **~40–50 redundant full re-uploads ≈ 100–120 MB of extra texture traffic
   plus as many redundant full mip regenerations per ~90 s pan session**, triggered by ~3 MB of
   (compressed) neighbor-tile arrivals; with cell-granular uploads the redundant transfer would
   be ~13 MB (mip regen would remain). Since all mosaics flagged by one arriving tile are bound
   in the same frame, these cluster as multi-upload frame spikes (up to 8×(2.36 MB upload + mip
   regen) in one frame; measured single uploads up to 20 ms under llvmpipe).

### 2.3 Network volume while panning

Per newly-exposed tile column at z11–14 (viewport ~5×4 tiles): ~4 OSM tiles (~125 KB) + ~4 visible
elevation tiles + ~5–6 new ring tiles = **~9–10 × 62 KB ≈ 600 KB elevation vs ~125 KB OSM**.
Elevation is ~4–5x the vector byte volume when the area is fresh; the 2-year elevation cache makes
revisits nearly free. The +75% prefetch amplification is real but bounded (ring only) — the
catastrophic Phase-4 congestion (mosaic per prefetch tile) is already fixed by the `isProxy()`
skip + shared `m_mosaics` registry.

### 2.4 Clean release-build (-O2) summary table

Strays verified killed between runs, all warm-disk unless noted:

| session                    | OSM tiles | build avg | p50  | p90   | max  | gridtess total/share |
|----------------------------|-----------|-----------|------|-------|------|----------------------|
| G: 2D, z11                 | 70        | 4.2 ms    | 1.8  | 13.1  | 30   | — (not invoked)      |
| F: 3D, shading off, z11    | 30        | 12.3 ms   | 6.3  | 33.7  | 67   | 131 ms / 35%         |
| E: 3D, shading on, z11     | 65        | 12.4 ms   | 8.1  | 21.7  | 84   | 392 ms / 49%         |
| H: 3D, shading on, z14 dt  | 52        | 47.4 ms   | 22.1 | 102.3 | 357  | 650 ms / 26%         |

Elevation-side (E): decode 0.6 ms avg; stitch 1.26 ms avg × 63 = 79 ms main-thread; 104 uploads
for 62 mosaics, 2.3 ms avg / 20 ms max (llvmpipe); DB reads 0.17–0.34 ms avg.

### 2.5 Texture shading does NOT starve the tile workers (anymore) — corrected finding

An intermediate chained-session comparison suggested texture shading slowed vector builds ~3x
via CPU contention. That was a **measurement artifact**: a leftover app instance from the
previous session (unreliable shutdown in the run script) kept spinning during later sessions.
With hardened runs, per-tile OSM build cost is **identical with texture shading on vs off
(12.4 vs 12.3 ms avg)** on this 8-core desktop. Texture shading's real, additive costs are those
in §2.2: +75–80% elevation fetch/decode volume, ~80–120 ms of main-thread stitching per pan
session, and the patch/re-upload/mip-regen storm on the render thread — these matter for frame
pacing and on weaker hardware, but the historical Phase-4 tile-worker starvation is fixed (the
`isProxy()` mosaic skip + shared `m_mosaics` registry) and did not reproduce in any clean run.

---

## 3. Ranked recommendations

Ranked by (estimated impact) / (effort + risk). File references are to `tangram-es/core/src/...`
unless noted.

### R1. Cut `buildPolygonGrid` cost (biggest CPU win for panning with 3D terrain)

- **Evidence:** 26–49% of all vector-tile build CPU (release; 55–65% debug); 10.4x triangle
  amplification (177 k → 1.84 M triangles per z11 session); 3D terrain triples-to-quadruples the
  median vector build (1.8 → 8.1 ms z11 release) and grid tessellation is roughly half of that
  3D cost. This hits every pan with 3D terrain, texture shading on or off.
- **Expected win:** ~1.5–2x faster vector tile builds in 3D (more on landuse-heavy tiles), i.e.
  tiles appear that much sooner on 2 workers; also ~10x fewer output triangles to upload/draw
  where the adaptive-resolution step applies.
- **Effort:** medium. **Risk:** low–medium (visual: draping fidelity).
- **Sketch (in order of value):**
  1. *Index interior cells arithmetically instead of hashing.* In
     `util/builders.cpp:buildPolygonGrid`, full-cell emission goes through `emitVertex`'s
     `unordered_map<uint64_t,uint16_t>` (3 lookups × 1.84 M triangles). Grid-lattice corners can
     be mapped to a `(gridRes+1)²` direct-index table (`std::vector<uint16_t>` per polygon,
     lazily filled); keep the hash map only for clip-generated (non-lattice) vertices. Hash
     traffic drops ~an order of magnitude for interior-dominated polygons.
  2. *Adaptive resolution per polygon.* Cap total cells per polygon: for a polygon whose bbox
     spans `w×h` cells, if `w*h > N` (e.g. 1024), halve the effective res (use 32 or 16) for that
     polygon only. Big forest/residential polygons at high zoom are exactly where the 64-grid
     explodes and where a coarser grid is least visible. Caveat: interior cells then no longer
     match the terrain mesh diagonal exactly (the code's own comment explains why matching
     matters), so keep the full res whenever the polygon covers < N cells, and accept approximate
     draping on huge polygons (they already get correct per-vertex elevation, just coarser).
     Wiring exists: `terrain_grid` already accepts an integer (`sceneLoader.cpp:994-1008`,
     `polygonStyle.cpp:115`, `assets/scenes/stylus-osm.yaml:401`).
  3. *Skip clipping for triangles that only cross one grid line* (common for thin earcut slivers):
     split once instead of running the full column×row Sutherland–Hodgman cascade. Micro-opt;
     do after 1–2 if still needed.
  4. *(Measure-first idea, not confirmed)* skip/coarsen the grid on genuinely flat tiles.
     Rugosity per elevation tile is already computed (`computeRugosity`, `rasterSource.cpp`), but
     vector tile building runs on workers, often before the matching elevation tile exists, and
     `RasterSource::m_mosaics/m_textures` are not thread-safe — needs a synchronized
     tile-rugosity side map. Only pursue if 1–2 aren't enough.

### R2. Batch/debounce mosaic patches; stop full re-upload + mip-regen per patch

- **Evidence:** 1.7–2.2x re-upload amplification (up to 4x per mosaic), ~100–120 MB of redundant
  GL traffic + 40–50 redundant full mip regens per 90 s pan session; clustered multi-upload
  frames (up to 8 in one frame, 20 ms per upload measured under llvmpipe).
- **Expected win:** ~50% fewer mosaic uploads/mip regens immediately; with per-frame coalescing,
  worst-case frames go from 8×(upload+mips) to 1–2. Biggest render-thread win on mobile.
- **Effort:** small–medium. **Risk:** low (bounded staleness of *coarse shading bands* only).
- **Sketch:**
  - In `RasterSource::patchNeighborMosaics` (`data/rasterSource.cpp`), don't call
    `mosaic->resize()` per patch. Instead mark the mosaic dirty in a small
    `std::vector<std::weak_ptr<Texture>> m_dirtyMosaics` (main thread only — same thread-safety
    envelope as the rest of the mosaic code).
  - Flush once per `Map::update()` (or from `TileManager::updateTileSets` via a small hook on the
    elevation `RasterSource`): for each dirty mosaic, call `resize()` once. Multiple neighbors
    arriving in the same frame → one re-upload instead of up to 8.
  - Optional stage 2: debounce with a deadline — flush a mosaic only when (a) its ring became
    complete, or (b) ≥150–250 ms since its first unflushed patch. Mirror-extrapolated coarse
    bands persisting an extra 200 ms is visually invisible (they were wrong for seconds before
    the patch anyway).
  - Optional stage 3 (bigger): partial upload via `glTexSubImage2D` for the patched 256×256 cell
    instead of full `glTexImage2D`. Requires a dirty-rect field on `Texture`
    (`gl/texture.h/.cpp`, `upload()`); mip regen still has to run over the whole texture, so do
    this only if profiling on device shows the 2.36 MB transfer (not the mip regen) dominating.

### R3. Raise `num_tile_workers` above 2

- **Evidence:** all vector builds + all raster decodes serialize onto 2 niced threads
  (`tile/tileWorker.cpp`, `WORKER_NICENESS 10`). At z14 downtown a single tile build is
  p50 22 ms / p90 102 ms / max 357 ms (release); a fast pan revealing ~20 tiles ≈ 1–2 s of build
  work queued on 2 workers while 6 of 8 cores idle — this queueing delay, plus network latency on
  fresh areas, *is* the perceived tile lag.
- **Expected win:** near-linear reduction in tile-appearance latency during pan bursts on
  desktop (2 → 4 workers ≈ half the wait). No reduction in total CPU (pair with R1).
- **Effort:** trivial — default in `app/src/mapsapp.cpp:807`
  (`cfg()["tangram"]["num_tile_workers"].as<int>(2)`): use e.g.
  `clamp(hardware_concurrency - 2, 2, 6)` as the default, keep the config override.
- **Risk:** low on desktop (each worker owns a `TileBuilder`; memory per builder is modest).
  On mobile keep 2 (or gate by core count) — thermal/foreground-thread contention.
  Note the comment there: single worker is used for debugging.

### R4. Bound main-thread stitching with the existing lazy-upgrade path

- **Evidence:** 52–63 stitches × 1.3–2.3 ms (release) on the main thread per session, bursty
  (several in one frame, and stacked on top of same-frame mosaic uploads) → frame hitches while
  panning; on slower hardware the per-stitch cost scales with memory bandwidth.
- **Expected win:** eliminates stitch-induced frame spikes; spreads work across frames.
- **Effort:** small. **Risk:** low — the fallback mechanism already exists and is exercised.
- **Sketch:** in `RasterTileTask::addRaster()` (`data/rasterSource.cpp`), consult a per-frame
  stitch budget (e.g. 2 stitches or ~4 ms, counter reset in `Map::update`, exposed via
  `RasterSource`). Over budget → attach the plain per-tile texture (exactly what proxy tiles do
  today) and let `TileManager::upgradeElevationMosaic()` (`tile/tileManager.cpp:683`) swap in the
  mosaic on a subsequent frame — that path was built for prefetch/proxy promotion and runs every
  update already. Do *not* move stitching to workers: `RasterSource::m_textures`/`m_mosaics` are
  documented main-thread-only, and making them thread-safe is more risk for the same payoff.

### R5. Prefetch hygiene: failure backoff, settle gating

- **Evidence:** prefetch dedup works (bounded +75% elevation fetches, ring-only — confirmed, so
  the "8x amplification" fear is refuted for steady state). Two real defects remain:
  1. **Per-frame retry of failed prefetches.** A permanently-failing neighbor (404 at data edge,
     offline, server error) has its entry erased on completion and re-enqueued *next frame*,
     forever (acknowledged as a "known limitation" in the comment above
     `enqueueNeighborPrefetch`, `tile/tileManager.cpp:561-565`). Offline or at data edges this is
     an unbounded request loop at render rate.
  2. Prefetches launched for ring tiles that a fast pan abandons within a second still run to
     completion (in-flight prefetch entries are deliberately kept alive,
     `keepPrefetching` in `updateTileSet`), each patching up to 8 mosaics (→ R2 uploads) on
     arrival.
- **Expected win:** removes pathological network churn offline/at edges; fewer wasted fetches +
  patch-uploads during fast pans. Modest in the common case.
- **Effort:** small. **Risk:** low.
- **Sketch:**
  - Failure backoff: keep a small `std::map<TileID, uint8_t> m_prefetchFailures` (or time-stamped
    set) per `TileSet` or on the `RasterSource`; increment when a prefetch task completes
    canceled/dataless; skip re-enqueue after N=2 failures for T=60 s.
  - Settle gating: skip `enqueueNeighborPrefetch` while the view is actively moving
    (`ViewState` has zoom/position deltas available in `updateTileSets`; or gate on
    `m_tilesInProgress > threshold`). The ring only matters once the center tiles are up anyway —
    patches land within a frame or two of settle.
  - Cancel-on-pan-away (optional): drop `keepPrefetching` retention when the entry's tile is no
    longer within 1 tile of any visible tile; its curl request is already cancelable via
    `clearTask()` → `cancelLoadingTile()`.

### R6. Free the mosaic CPU buffer once its ring is complete (memory)

- **Evidence:** every live mosaic keeps: 2.36 MB CPU stitch buffer (`disposeBuffer=false`, kept
  for patching) + ~3.1 MB GPU (768² float + mips) + the 264 KB original tile texture pinned via
  `mosaic->userData` (`ElevationMosaicInfo::ownTexture` — needed so later neighbors can stitch
  real data; originals are effectively CPU-only — GPU upload happens only in the brief window
  where a tile renders with the plain texture — so they're the cheap part). ~63 live mosaics
  in a normal session ≈ **150 MB CPU + ~195 MB GPU** for elevation alone, vs ~17 MB pre-mosaic.
  Memory pressure → built-tile cache eviction → re-fetch/re-build churn is a real risk on mobile
  (512 MB `tile_cache_limit` is generous relative to phone budgets).
- **Expected win:** ~150 MB CPU RAM at steady state (the GPU side needs the deferred shared-atlas
  redesign — out of scope, see "not worth it").
- **Effort:** small–medium. **Risk:** low–medium (must not break patching or seam fixes).
- **Sketch:** add a `uint8_t realCellMask` to `ElevationMosaicInfo` (`data/rasterSource.h`), set
  bits in `stitchElevationMosaic`/`patchNeighborMosaics`. When all 8 neighbor bits are set *and*
  the texture has been uploaded (`Texture` would need a "was uploaded at least once" query, or
  flush via the R2 dirty list first), free the CPU buffer (`Texture::m_buffer`). Keep
  `ownTexture` pinned — it must live as long as the mosaic (its CPU buffer is what future
  neighbor stitches copy from; freeing it re-introduces the permanent-mirror seam bug fixed on
  this branch). `patchNeighborMosaics` already no-ops when `bufferData()` is null.

### R7. Elevation fetch priority vs. vector tiles (only if vector sources go networked)

- **Evidence:** `UrlClient` (`platforms/common/urlClient.cpp`) is a plain FIFO with 20 slots; it
  has no notion of `TileTask` priority, so a burst of elevation ring prefetches can briefly queue
  ahead of just-visible vector tiles. Not measurable in this setup (OSM was offline-cached);
  worth doing only when a network vector source is in play.
- **Sketch:** two-tier queue in `UrlClient` (normal/low), `startUrlRequest` variant carrying a
  low-priority flag from `NetworkDataSource::loadTileData` when `task->isProxy()`.
- **Effort:** small–medium. **Risk:** low. Priority: low until relevant.

---

## 4. Explicitly *not* worth it (checked, refuted, or deferred)

- **SQLite cache contention / synchronous writes** — measured cheap on desktop: reads avg
  0.2–4.6 ms (incl. the per-read `tile_last_access` autocommit write), stores ~31–62 KB in their
  own transaction on the source's dedicated AsyncWorker thread, never blocking workers or the
  main thread. WAL (`mbtilesDataSource.cpp:412`, deliberately disabled) would help mostly on
  slow flash / mobile — revisit only with on-device profiles. A cheap hygiene option if ever
  needed: batch `putLastAccess` updates (accumulate tile_ids, flush one transaction every few
  seconds).
- **`computeRugosity` / `aggregateRugosity`** — 0.04 ms per stitch and a ~60-entry map walk per
  frame respectively. Noise.
- **Stitch memcpy micro-optimization** — the 9-cell copy is memory-bandwidth bound and already
  row-wise `memcpy`; the win lives in *when/where* stitching happens (R4), not in the copy.
- **Dedup of neighbor prefetch enqueues** — already correct: `visibleTiles`/`tiles`/texture-cache
  checks bound it to the viewport ring. The remaining issues are failure retry + settle gating
  (R5), not duplication.
- **Shared cross-tile mosaic atlas (removing the ~9x elevation texture redundancy)** — still the
  right long-term answer for GPU memory and for eliminating patch-uploads entirely, and still
  correctly deferred: it invalidates the per-tile raster binding model and the seam guarantees
  built on aligned per-tile mosaics. Do R2/R6 first; they capture most of the practical cost.
- **LERC decode / MVT parse** — 0.6–2.3 ms (release) and a minority of `process()` time
  respectively; the style/geometry stage dominates.
- **`enqueueNeighborPrefetch` per-frame scan cost** — ~30 visible tiles × 8 map/cache lookups per
  frame; microseconds. (The `LOGD` "already cached" lines fire hundreds of times per session in
  debug builds but compile out at release `LOG_LEVEL=2`.)

## 5. Suggested order of implementation

1. **R3** (worker count default) — one line, immediate pan-latency relief on desktop.
2. **R2** (patch coalescing, stage 1+2) — small, kills the upload/mip-regen storm.
3. **R1.1 → R1.2** (grid tessellation) — the big CPU win; measure after each step with the same
   pan scenario (§1) before going further down R1's list.
4. **R4** (stitch budget via `upgradeElevationMosaic`) — small, removes frame hitches.
5. **R5** (prefetch backoff + settle gating) — small, network hygiene.
6. **R6** (free completed mosaic CPU buffers) — before any mobile push.

A rerun of the §1 pan scenario (sessions E and H equivalents, with the §1 stray-process
precaution) after 1–3 should show: OSM build p50 ~1.5–2x lower in 3D, mosaic uploads ≈ distinct
mosaics (amplification →1.0x from the current 1.7–2.2x), and no multi-upload frames.
