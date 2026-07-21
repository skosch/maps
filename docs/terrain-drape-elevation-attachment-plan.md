# Terrain-drape elevation attachment — stuck ancestor-fallback investigation & fix options

**Status:** root cause confirmed (see "Round 4 update" at the bottom) — texture shading being ON
(`global.elevation_mosaic: true`, currently persisted in `~/.config/Ascend/config.yaml`) silently
disables the overzoom-composite mechanism for the drape, freezing `getTerrainMeshElevation()`'s
lattice resolution at a non-overzoom-aware 64 cells. No fixes implemented yet — **read the "Round
4 update" section first; it supersedes everything above it in this document.** The original §1-§2
mechanism (raster pinned to a coarser ancestor via `tileManager.cpp`'s per-frame healing pass) is
**retired** — the direct CPU-side LOGD check found zero hits, and Round 3's "darker gray" visual
evidence that motivated it turned out to be a methodological mistake (hillshade's own shading, not
a real reading of `u_raster_offsets.z`). Kept for history/context only. Written per Sebastian's
"research first, present plan, implement only after sign-off" workflow. **Do not implement any of
the Round 4 candidate fixes without explicit approval — and see Round 4's recommended zero-code
confirmation step (texture shading off, re-run the contour comparison) before committing to one.**

## What changed since the previous doc

The previous investigation (`docs/terrain-drape-resolution-plan.md`) concluded the `bare_rock`
"waterline" near the Matterhorn summit was caused by a hard, shared resolution ceiling
(`terrain_grid: 256` / `kOverzoomCompositeMaxLevels: 2`) that both the polygon drape mesh and its
elevation reference lattice hit simultaneously, by design, once display zoom reached roughly
z16-equivalent. Sebastian and the team tested this directly:

1. **4x resolution bump** (`elevation.yaml` `max_zoom` 16→18, `rasterSource.cpp`
   `kOverzoomCompositeMaxLevels` 2→4, `stylus-osm.yaml` `terrain_grid` 256→1024, full rebuild of
   both configs) — **zero visible effect**. This directly refutes the ceiling theory: if the bug
   were a resolution/tessellation-density problem, more resolution headroom would have measurably
   pushed the "waterline" further out. It didn't move at all. (Fully reverted; tree confirmed
   clean.)
2. **Simultaneous diagnostic recolor** (`bare_rock` blue, `terrain-ground` magenta) — the gap area
   reads **pure magenta**, confirming terrain-ground's own mesh/elevation is present and correct
   there; the failure is specific to the `bare_rock` polygon's own drape losing the z-test against
   the (correct) terrain-ground surface, not a third, independent mechanism.
3. **Constant `elev3d` nudge** in the vector-polygon branch of `terrain-3d.yaml`'s `position`
   block (non-raster branch only) — +50 m: no visible effect; **+1000 m: the gap disappeared
   entirely**, floating ~1000 m above the real (confirmed-correct) terrain underneath. This is the
   decisive result: the polygon's elevation read is not merely *less detailed* than the true
   surface (which a bump of a few tens of meters would mostly paper over) — it is **wrong by
   ~1000 m**, the signature of averaging over a much coarser DEM tile, not of a piecewise-linear
   interpolation gap between correctly-anchored corners. A resolution-only theory cannot produce a
   four-figure-meter error; a wrong-zoom-tile-averaging theory can.
   - A small (~100-200 m) residual gap right at the very summit tip, unaffected by the main bug,
     reappeared at a smaller scale on zooming into just that tip — this is the pre-existing,
     already-accepted "~1% at cols/summits" PL-interpolation residual from
     `docs/terrain-illumination-plan.md`, unrelated to the main bug; don't conflate the two.
4. **Live diagnostic**: overriding `bare_rock`'s color block to visualize
   `u_raster_offsets[ELEVATION_INDEX].z` as grayscale (currently still active, uncommitted, in
   `assets/scenes/stylus-osm.yaml` — marked "TEMP DIAGNOSTIC," left in place per Sebastian's
   request) shows the broad waterline-affected area reading **visibly darker gray** than
   surrounding unaffected slopes (which read white, i.e. `z ~= 1.0`), and the darker region
   **grows** with continued zoom/tilt, matching the bug's footprint. Confirmed **stable at rest**
   (didn't resolve after several seconds sitting still) — ruling out ordinary fetch-latency lag.

This section replaces the previous "confirmed root cause" with the corrected mechanism below,
fully traced through the C++ side that the first round did not cover (`u_raster_offsets` itself).

## Confirmed mechanism, with code evidence

### 1. What `u_raster_offsets[idx].z` actually is, and what a value below 1.0 means

Set in `Style::setupTileShaderUniforms` (`tangram-es/core/src/style/style.cpp:240-282`):

```cpp
TileID tileID = _tile.getID();
...
for (auto& raster : _tile.rasters()) {
    ...
    float x = 0.f, y = 0.f, z = 1.f;
    if (tileID.z > raster.tileID.z) {
        float dz = tileID.z - raster.tileID.z;
        float dz2 = powf(2.f, dz);
        x = fmodf(tileID.x, dz2) / dz2;
        y = (dz2 - 1.f - fmodf(tileID.y, dz2)) / dz2;
        z = 1.f / dz2;
    }
    rasterOffsetsUniform.emplace_back(x, y, z);
}
```

`tileID` is the draped vector tile's own `TileID` (`(x, y, z=14, s)` for an `osm` tile — `z` here
is fixed at the vector source's own native data zoom, 14, regardless of how overzoomed `s` is).
`raster.tileID` is whatever `TileID` the *attached elevation raster* is currently registered
under. **`z` (the uniform component `getTerrainMeshElevation()` reads, per its own doc comment in
`elevation.yaml`) is 1.0 whenever `raster.tileID.z == tileID.z` — true both for an exact-zoom
match AND for a same-footprint overzoom composite, which the earlier "white patches" work
deliberately registers at the primary's own coarse `z` precisely so this check stays a no-op (see
`docs/terrain-illumination-plan.md`'s "third phenomenon" fix).** `z` drops to a fraction
(0.5, 0.25, ...) **only** when `raster.tileID.z < tileID.z` — i.e. the attached raster is a
genuinely coarser **ancestor** tile, not this vector tile's own footprint at any resolution.

**So Sebastian's diagnostic (darker gray, growing, stable at rest) is a direct, unambiguous read
of `raster.tileID.z < 14` for the affected tiles — a real ancestor-tile pin, not a resolution
artifact.** This also explains the "wrong by ~1000 m" magnitude from test 3: the actual elevation
*values* sampled by `getElevationAt()`/`getTerrainMeshElevation()` come from that same coarser
ancestor texture — a real DEM tile several zoom levels out, whose native resolution genuinely
smooths away the true summit height (a sharp Alpine peak reads systematically far lower once
averaged over a ~4x-8x-larger pixel footprint) — this is a wrong-**value** bug, not a
tessellation-density one, exactly matching why the resolution bump in test 1 did nothing and the
constant-height nudge in test 3 fixed it outright.

### 2. Where a raster gets pinned to a coarser ancestor, and why it's a *dead end* once pinned

The only place found that walks up to a coarser ancestor and assigns it to `raster.tileID` is
`TileManager::updateTileSet`'s "missing-raster" pass (`tangram-es/core/src/tile/tileManager.cpp:
599-651`), which runs once per visible tile whenever `entry.numMissingRasters != 0`:

```cpp
TileID intended = (zoomDiff > 0)
        ? tileId.zoomBiasAdjusted(zoomDiff).withMaxSourceZoom(srcs[ii]->maxZoom())
        : tileId.withMaxSourceZoom(srcs[ii]->maxZoom());   // == tileId itself: 14 <= elevation maxZoom 16, no-op
bool missing = raster.texture == srcs[ii]->emptyTexture();
if (!missing && raster.tileID.z >= intended.z) { continue; }   // already fine, nothing to do
if (auto exact = srcs[ii]->getTexture(TileID(intended.x, intended.y, intended.z))) {
    raster.tileID = TileID(intended.x, intended.y, intended.z, tileId.s);   // HEALED to exact z=14
    raster.texture = exact;
    continue;
}
++entry.numMissingRasters;
if (!missing) { continue; }   // already showing a parent; keep it until healed
TileID id(intended.x, intended.y, intended.z);
do {
    id = id.getParent();                      // z=13, then z=12 (see exit condition below)
    auto proxy = srcs[ii]->getTexture(id);
    if (proxy) {
        raster.tileID = TileID(id.x, id.y, id.z, tileId.s);   // PINNED to ancestor
        raster.texture = proxy;
        break;
    }
} while (id.z > 13 || (id.z > 0 && id.z + 2 >= tileId.z));   // for tileId.z=14: only tries z=13, z=12
```

Two things confirmed directly from this code:

- **The ancestor search is hard-capped at exactly 2 levels above the vector source's own native
  zoom** (14→13→12 for `osm`), via the loop's exit condition — it is *not* tied to
  `kOverzoomCompositeMaxLevels` or to how overzoomed the current view actually is; it's a fixed,
  small search depth regardless. If neither z=13 nor z=12 happens to be cached, the raster is left
  exactly as it was (still `missing`/still pinned from a prior pass).
- **Everything this pass does to recover from "missing" is a passive cache lookup**
  (`srcs[ii]->getTexture(...)`, a `weak_ptr::lock()` against `RasterSource::m_textures` —
  `rasterSource.cpp:800-802`). Nothing here — or anywhere else found in the codebase — **actively
  re-issues a fetch** for the missing exact-zoom (x, y, 14) tile. The "still counted as missing"
  bookkeeping (`entry.numMissingRasters` stays incremented after a parent pin) means this pass
  keeps *re-checking* the same cache slot every frame forever, hoping something else populates it
  — but never causes anything to populate it.
- Separately, **`TileManager::upgradeAttachedRasters` (the continuously-running composite-upgrade
  pass examined in the previous investigation) never touches `raster.tileID`, only
  `raster.texture`** (`tileManager.cpp:967-1160`, confirmed by direct reading — every assignment in
  that loop is `raster.texture = mosaic/composite`, never `raster.tileID = ...`). So even if it
  manages to build a nicer composite starting from the pinned ancestor's own coordinates, the
  shader's crop-fraction math and the composite's own achievable ceiling
  (`overzoomTargetZoom` computed from the *ancestor's* z, one or two levels worse than it should be)
  both stay permanently degraded once a raster is pinned to an ancestor. **There is no code path
  found anywhere that un-pins `raster.tileID` back up to the vector tile's own native zoom, other
  than the exact-match healing check (`getTexture(TileID(intended.x, intended.y, intended.z))`,
  line 629 above) finding the true (x, y, 14) tile in cache on some later frame.**

### 3. Confirmed: this can only be reached via genuine cancellation, not ordinary fetch latency

It would be easy to assume this is just "the elevation fetch hasn't finished yet" — but
`TileEntry::completeTileTask()` (`tileManager.cpp:89-97`) explicitly rules that out by design:

```cpp
bool completeTileTask() {
    if (bool(task) && task->isReady()) {
        for (auto& subtask : task->subTasks()) {
            if (!subtask->isReady() && !subtask->isCanceled()) { return false; }  // wait longer
        }
        task->complete();
        ...
```

The parent vector tile is not considered "ready to complete" until **every** raster subtask
(including the elevation attachment) is either ready or **canceled**. So the empty-texture /
ancestor-fallback path in `RasterTileTask::complete(TileTask&)` (`rasterSource.cpp:655-663`,
`if (!isReady()) { ...attach emptyTexture()... }`) can only be reached when the subtask was
actually canceled — a simple "still loading" race is excluded by this guard.

### 4. Leading hypothesis for *why* the subtask gets canceled (flagged as needing runtime confirmation)

`TileEntry::clearTask()` (`tileManager.cpp:110-122`) cancels a task's subtasks
(`subtask->source()->cancelLoadingTile(*subtask)`) when a `TileEntry` is destroyed (a superseded
entry falling out of `TileSet::tiles`, e.g. `tiles.erase(...)` at `tileManager.cpp:686` when a tile
is no longer visible and not needed as a proxy). This is routine and expected during ordinary
tile churn — **but during a fast, continuous zoom-in gesture that crosses several overzoom-step
`s` thresholds in quick succession** (established in the previous doc: `s` climbs roughly once per
view-zoom-level once a source hits its own `max_zoom`, and each new `s` value is a brand-new
`TileID`/cache key, triggering a brand-new `TileTask` and a brand-new elevation raster subtask) —
**exactly the kind of rapid zoom+tilt testing used to chase this bug on the Matterhorn** — an
intermediate `s` value's vector tile (and its freshly-spawned elevation subtask) can be built and
then torn down again before that subtask finishes loading, if the view moves on to yet another `s`
before it completes.

If the *very first* time this exact tile footprint ever needed its own native-zoom (x, y, 14)
elevation texture happens to land in this churn, that fetch is canceled before
`RasterSource::cacheTexture()` (`rasterSource.cpp:765-798`) ever runs for it — meaning the decoded
texture never even enters `RasterSource::m_textures` in the first place. And because
`m_textures` holds only **weak** references with a custom deleter that erases the cache entry
the instant the last strong `shared_ptr` disappears (`cacheTexture`'s deleter,
`rasterSource.cpp:777-784`), there is no other tileset that would independently keep this *exact*
(x, y, 14) footprint's texture alive or re-populate it later — `terrain-ground`'s own tileset
targets different (finer, e.g. z15/z16) tiles for the same ground area, not this specific
coordinate, and no other vector tile shares this exact footprint. Once the ancestor pin from
§2 takes hold, the passive-only healing check has nothing left to ever find.

**This chain (rapid-zoom-churn cancellation → weak-ptr cache never populated for this exact
tile → passive-only healing pass permanently checking an cache slot nothing will ever fill) is
consistent with every observed symptom**: broad area (a whole z14 `osm` tile's footprint, ~2+ km
across at these latitudes, all pinned to the same coarse ancestor at once), growing with continued
zoom/tilt (more of a fixed, large, real elevation error becomes visible/occupies more of the
screen — and, plausibly, more neighboring tiles independently suffer the same churn-induced
cancellation the longer/faster the zooming continues), and stable at rest (nothing is retrying).

**What is not yet runtime-confirmed** (flagged honestly, since I did not run the app per this
project's visual-verification policy): whether cancellation-during-rapid-zoom-churn is *really*
the trigger, versus some other reason the exact fetch never resolves. The existing code already
has `LOGD` instrumentation on exactly this path (`"Healed '%s' subtask raster..."`,
`"Found proxy %s for missing..."`, `tileManager.cpp:632-633,646-647`) — Sebastian re-running a
repro with a Debug build and log level turned up, watching whether "Healed" ever fires for the
affected tile coordinates, would directly confirm or redirect this diagnosis before any fix is
implemented. This is a cheap, concrete next step and matches the project's established preference
for Sebastian generating diagnostic data himself (`docs/profiling-plan.md` workflow).

### 5. Ruled out / not the mechanism

- The resolution-ceiling theory from the previous doc (refuted by test 1 above).
- Ordinary fetch latency (ruled out by `completeTileTask()`'s wait-for-subtask guard, §3, and by
  "stable at rest" not resolving after several seconds — latency numbers in
  `docs/tile-pipeline-perf-plan.md` are tens to low-hundreds of ms even over cold network, nowhere
  near multi-second).
- `RasterStyle`/terrain-ground's own elevation and mesh (directly confirmed correct by the
  magenta-recolor test).

## Candidate fix directions

### (a) Make the healing pass actively re-fetch, not just passively re-check

Instead of only checking `srcs[ii]->getTexture(...)` (a cache-lookup no-op if absent), have the
"missing-raster" pass in `updateTileSet` issue a genuine, deduplicated fetch request for the
intended exact-zoom tile when it finds `raster.texture == emptyTexture()` or a parent pin — the
general mechanism already exists elsewhere (`_tileSet.source->createTask(...)` is used for
ordinary tile loading in the same function) and would need to be adapted for a raster-only,
subtask-style fetch that patches into the already-attached `Raster` once it completes, similar to
how `enqueueNeighborPrefetch` already manages background elevation fetches outside the main tile
lifecycle.

- **Costs:** the most invasive of the options — needs new bookkeeping to avoid issuing duplicate
  fetches every frame while one is already in flight, and to route the eventual result back into
  the correct `Tile`/`Raster` slot (which may itself have been superseded again by then). This is
  exactly the class of terrain-drape code with a history of subtle regressions
  (`docs/terrain-illumination-plan.md`'s three "white patches" rounds); any change here needs the
  same before/after screenshot discipline used there.
- **Benefit:** directly closes the confirmed gap (§2) — a raster that's ever missing/pinned would
  have a real, bounded path back to correctness, instead of depending entirely on something else
  in the app coincidentally populating that exact cache slot.
- **Risk:** medium-high, given the history of this code area.

### (b) Reduce/avoid subtask cancellation churn during rapid zoom-in bursts

If §4's hypothesis holds, the more surgical fix is upstream of the healing pass entirely: don't
tear down an in-flight elevation subtask just because its parent vector `TileEntry` was superseded
by a newer `s` value mid-flight. E.g. let a canceled parent's still-in-flight raster subtask keep
running to completion in the background (it would still land in `RasterSource::m_textures` via
`cacheTexture()` once done, available for the *next* `s` value's tile to pick up synchronously),
or add a short grace period before `clearTask()` actually cancels a subtask that's already partway
loaded.

- **Costs:** needs care not to reintroduce the exact tile-worker congestion the Phase 4
  texture-shading work fixed (`docs/tile-pipeline-perf-plan.md`, `docs/texture-shading-plan.md`) —
  letting more subtasks run to completion during fast panning/zooming was previously a source of
  worker-pool starvation.
- **Benefit:** addresses the likely root trigger directly, and is a narrower, more contained change
  than (a) — doesn't need new active-fetch/patch-back machinery, just changes *when* an
  already-legitimate fetch gets torn down.
- **Risk:** medium — behavior change to a shared cancellation path (`TileEntry::clearTask()`) used
  by every tile source, so needs testing across ordinary panning too, not just this bug's specific
  repro.

### (c) Keep a small strong-ref pin for "this exact tile's own native-zoom elevation," independent of transient attachment

Add a small, bounded LRU of strongly-held elevation textures keyed by the *vector* tile's own
native (x, y, z) coordinates (separate from the ordinary `m_textures` weak-ref cache), populated
whenever a fetch for that exact coordinate succeeds — regardless of whether the vector tile that
requested it is still around to hold the `Raster` reference by the time it completes. This
directly prevents the weak-ptr eviction described in §4 without touching the cancellation timing
in (b) or building new active-fetch machinery as in (a).

- **Costs:** another cache with its own sizing/eviction policy to tune and reason about
  (mirrors the existing `RasterSource::m_overzoomMosaics` blunt-clear-at-128-entries pattern) —
  modest but real added complexity.
- **Benefit:** cheapest of the three to reason about in isolation — a pure addition, doesn't change
  existing cancellation or fetch-triggering logic at all, so lower regression risk to unrelated
  panning/zoom behavior.
- **Risk:** low-medium; mainly needs a sane eviction bound so it doesn't just become an unbounded
  memory leak of every distinct z14 tile ever visited in a long session.

## Recommendation (Sebastian's call, not a decision)

Given (3) confirms this can only happen via cancellation, and (4) has a concrete, plausible trigger
(rapid-zoom-churn destroying the very first in-flight fetch for a given tile footprint) that is
cheap to confirm or refute via the existing `LOGD` lines, my recommendation is to **do that
confirmation step first** — it's nearly free and would immediately tell us whether to invest in
(b)/(c) (churn-avoidance / eviction-avoidance, if cancellation is indeed the trigger) or pivot
entirely (if "Healed" logs show the exact tile WAS found and attached, in which case the bug is a
distinct, not-yet-found issue in the healing check's own logic, and none of the three options above
would be right). Contingent on that confirmation, my inclination is (c) — a small strong-ref pin —
as the best cost/risk tradeoff: it's the narrowest, most contained change of the three, doesn't
touch the shared cancellation path every other tile source also depends on (unlike (b)), and
doesn't need new fetch-deduplication/patch-back machinery (unlike (a)). But this is a
recommendation for sign-off, not a decision — please confirm direction (or the confirmation step's
findings redirect this) before any implementation begins.

---

## Round 3 update (2026-07-19): the confirmation step gave a decisive negative — and it doesn't cleanly fit the traced mechanism either

Sebastian ran the recommended confirmation step: reproduced the bug on the existing Debug build
(`LOG_LEVEL=3`, confirmed already active — `Makefile:24`/`CMakeLists.txt:36-48` both default
`LOG_LEVEL` to 3 for Debug builds unless explicitly overridden), continued zooming/panning
afterward specifically to give any in-flight-fetch-cancellation mechanism every chance to fire,
then grepped the **entire** session log for `"Found proxy"` and `"Healed"` — **the two log lines
that are, per §2 above, the only place in `tileManager.cpp`'s per-frame healing pass that can ever
assign `raster.tileID` to a coarser value.** Neither string appears anywhere, despite confirmed
visual reproduction of the waterline (the `u_raster_offsets.z` grayscale diagnostic was visibly
present) and continued opportunity for the mechanism to fire.

### Re-tracing §2's specific claim: is there a construction-time path that bypasses `emptyTexture()` entirely?

I re-read every site in the codebase that constructs a `Raster` or assigns `Tile::rasters()`,
via an exhaustive grep for `Raster(`, `make_unique<Raster>`, and `rasters().emplace_back` across
`tangram-es/core/src` and `tangram-es/core/include`:

- `RasterTileTask::process()` (`rasterSource.cpp:536-541`) — only used for the raster source's
  *own* primary tiles (terrain-ground), not relevant to `osm`'s attachment.
- `RasterSource::createRasterTask()` (`rasterSource.cpp:735-755`) — on a cache hit, constructs
  `Raster(id, texture)` where `id = TileID(_tileId.x, _tileId.y, _tileId.z)` — **always the exact
  requested zoom** (14 for `osm`'s elevation subtask; `withMaxSourceZoom(16)` is a no-op for
  z=14≤16, so this is always exactly 14, never an ancestor).
- `RasterTileTask::addRaster()` (`rasterSource.cpp:545-643`) — on a successful fetch, constructs
  `Raster(m_tileId, tex)` — `m_tileId` is the subtask's own `_tileId` (`(x, y, 14, s)`), again
  always z=14. All three of its own re-emit paths (`overzoomTex`, `mosaicTex`, and the final plain
  fallback) preserve `raster->tileID` unchanged — confirmed once more, none of them touch `.z`.
- `RasterTileTask::complete(TileTask&)` (`rasterSource.cpp:655-663`) — the `!isReady()` branch
  attaches `source->emptyTexture()` at `tileId()` (again the subtask's own id, z=14, not an
  ancestor).
- `RasterSource::getRaster(ProjectedMeters)` (`rasterSource.cpp:1254-1266`) — **does** walk up to a
  coarser ancestor via `getParent()`, exactly like the coordinator's hypothesis describes — but
  this is a synchronous point-query helper used only by `ElevationManager`/`TextureShading`
  (`elevationManager.cpp:134`, `textureShading.cpp:207`) for camera-height lookups and the texture-
  shading pyramid, **not called anywhere in the per-tile raster-attachment pipeline**
  (`RasterSource::addRasterTask`/`TileSource::addRasterTasks`, confirmed by grep — no call site
  outside those two).

**Conclusion: I could not find a construction-time path that seeds a coarser ancestor directly.**
Every site that constructs or first assigns `Tile::rasters()` for a vector tile's elevation
attachment produces `raster.tileID.z == 14`, whether the underlying fetch succeeded or was
canceled. The **only** two assignments in the entire codebase that can ever produce
`raster.tileID.z < tileID.z` remain exactly the two lines already identified in §2
(`tileManager.cpp:630` and `:644`), both of which log. So the coordinator's specific proposed
mechanism ("initial construction already seeds a coarser ancestor, bypassing `emptyTexture()`
entirely") does not appear to exist in this codebase as I've traced it — I looked for it
specifically and could not confirm it.

### Why this leaves a genuine, unresolved contradiction rather than a clean pivot

This creates a real tension, not a tidy answer:

- If `u_raster_offsets[ELEVATION_INDEX].z < 1.0` is being observed (which the live diagnostic
  directly shows, as darker gray, growing with zoom/tilt), then by §2's exhaustive code trace,
  `tileManager.cpp:644` (or `:630`) **must** have executed for whichever tile is currently
  rendered there — and that line unconditionally logs. Zero log occurrences is hard to square with
  that.
- I also checked `TileEntry::completeTileTask()` (`tileManager.cpp:89-108`) for a subtlety that
  could explain "the pass ran, but stopped running before it could log": `numMissingRasters` is
  reset to `-1` (forcing a healing check) **only at the exact moment a TileEntry's task first
  completes** (line 103). If, on that very first check, the raster is already fine (`!missing &&
  raster.tileID.z >= intended.z` — i.e. the exact-zoom fetch actually succeeded, no cancellation),
  `numMissingRasters` stays `0` and this entire pass **never runs again for that entry** — meaning
  if some later, entirely different mechanism were to degrade the elevation reading, this pass
  wouldn't catch it (nothing would log, matching the evidence) — but I could not find any *other*
  mechanism that touches `raster.tileID` or otherwise degrades the elevation reading later (see the
  exhaustive re-trace above), so this doesn't resolve to a positive alternative, only to "the code
  I can find doesn't explain the symptom, either via the pinning path or via any substitute path."
- I also considered whether the darker-gray area could be a **whole-tile proxy** (an older, coarser
  `osm` Tile object — a genuinely different, self-consistently-built tile at a lower `z`, rendered
  in place of a still-loading finer one via the separate `m_proxyCounter` mechanism,
  `tileManager.cpp:541-565`) rather than a raster-level pin. This would plausibly explain
  genuinely-coarse elevation *values* (a real but low-resolution ancestor tile) without ever
  touching the raster-pinning code at all — but a self-consistently-built proxy tile's own
  `raster.tileID.z` would equal *its own* `tileID.z` (both coarser, but matching each other), which
  would make `u_raster_offsets[idx].z` read **1.0**, not the observed fraction. This doesn't fit
  the specific diagnostic reading either, so I'm not carrying it forward as the leading theory,
  though it's worth keeping in mind as a related, separate possible contributor to "wrong
  elevation values near the summit" independent of this specific uniform.
- One more data point worth noting: the earlier constant-elevation-nudge test found **+1000 m**
  (not, say, +4000 m) was enough to make the gap disappear entirely. If the attachment were the
  literal `emptyTexture()` (all-zero pixel, i.e. elevation reading exactly 0 m), a summit at roughly
  4478 m would need close to a 4000+ m nudge to fully compensate, not 1000 m. The magnitude of the
  needed nudge is more consistent with a **real, moderately-coarse elevation value** (a genuine
  ancestor tile smoothing away the peak, not literally zero) than with a totally-empty texture —
  which further supports "some real coarser texture is being read" over "the empty placeholder is
  stuck forever," even though I can't currently show how a real coarser texture gets attached
  without the log lines that didn't fire.

### What I'm not willing to do: guess past this

I don't have a single, code-confirmed root cause I can respons­ibly hand over as final right now —
the evidence points at "a coarser real texture is genuinely being read" (from the nudge-magnitude
test) while the only code path I can find that could attach one (the per-frame healing pass)
appears, per the log, to never have run its logged branches. Rather than pick one of the two
half-fitting theories above and present it as confirmed, I'm recommending one more, very cheap,
maximally-direct diagnostic that would resolve this decisively either way.

### Recommended next diagnostic (cheap, decisive, no fix code)

1. **First, rule out a log-capture gap** (the mundane explanation, and worth eliminating before
   trusting the "zero occurrences" result any further): check whether *any* `LOGD` output at all
   appears in the captured log (e.g. grep for a always-firing `LOGD`/`LOGV` line, or check the log
   file's start timestamp against when the app process actually started — if log capture began
   *after* the app was already launched and navigated toward the Matterhorn, any earlier "Found
   proxy"/"Healed" events from session startup would be silently missed). This is the single
   cheapest thing to double check and, if it turns out to be the gap, fully resolves the
   contradiction in favor of the mechanism already documented in §1-§2 above.
2. **If log capture is confirmed complete and correct**, add one new, small, temporary,
   *unconditional* diagnostic directly in `Style::setupTileShaderUniforms`
   (`tangram-es/core/src/style/style.cpp:262-270`) — not gated by `numMissingRasters` or any other
   state, firing every time `tileID.z > raster.tileID.z` is actually true at render time, logging
   both `tileID.toString()` and `raster.tileID.toString()`. This observes the *symptom itself*
   directly at the exact place it's read, sidestepping the question of which upstream mechanism
   produced it — if it fires, we get the real tile coordinates and can trace backward with actual
   data instead of hypothesizing; if it *never* fires even during a confirmed-reproduced waterline,
   that would mean my reading of the `u_raster_offsets.z` diagnostic itself needs to be
   reconsidered (e.g. double-check `ELEVATION_INDEX`'s value for the `rock-texture`/`unlit-polygons`
   style really does correspond to array index 0 as assumed, or that the grayscale debug override
   is reading the uniform Sebastian intended). Either outcome is decisive. This is a diagnostic
   addition only (mirrors the existing, already-in-place `u_raster_offsets.z` color override in
   spirit) — not a fix, and easily reverted.

I'm not landing on a single fix direction this round, per the above — picking one now would mean
guessing between two theories that don't both fit the same evidence, against this project's
explicit "don't implement/commit to unverified diagnoses" workflow. The diagnostic in step 2 should
be nearly as cheap as the log-grep already done and should let the next round land on one
confirmed mechanism and a single recommended fix.

---

## Round 4 update (2026-07-19): root cause confirmed — texture shading being ON silently disables the overzoom-composite mechanism for the drape, reverting `getTerrainMeshElevation()` to a frozen, non-overzoom-aware 64-cell lattice

**Round 2/3's `u_raster_offsets.z` / ancestor-fallback theory is retired**, confirmed by the direct
CPU-side LOGD check (unconditional, in `Style::setupTileShaderUniforms`, firing whenever
`tileID.z > raster.tileID.z`): zero hits. `u_raster_offsets[ELEVATION_INDEX].z == 1.0` for the
tiles in question — the earlier "darker gray" visual reading (Round 2) was a methodological
mistake (hillshade's own translucent shading over `bare_rock`, not a real read of that uniform).
Round 3's direct `getTerrainMeshElevation()` vs `getElevation()` contour-line comparison (red vs.
black, hillshade hidden) is the real, reliable signal: **they agree near the summit, then diverge,
non-parallel, moving away from it.**

### The confirmed mechanism

`getTerrainMeshElevation()`'s lattice-resolution derivation (`elevation.yaml`) infers how many
cells the currently-bound elevation texture spans from that **texture's own raw pixel
dimensions**:

```glsl
#ifdef ELEVATION_MOSAIC
    float effectiveWp = rasterPixelSize(ELEVATION_INDEX).x / 3.0;
#else
    float effectiveWp = rasterPixelSize(ELEVATION_INDEX).x - float(ELEVATION_TILE_OVERLAP);
#endif
float cells = 64.0 * effectiveWp / float(ELEVATION_TILE_PIXELS) * u_raster_offsets[ELEVATION_INDEX].z;
```

This design assumes the bound texture is **either** a plain per-tile texture **or** the
"overzoom composite" from `RasterSource::buildOverzoomElevationMosaic` (whose pixel width grows
with how overzoomed the view is, so `effectiveWp`/`cells` grows too — this is the mechanism the
earlier "third phenomenon" investigation in `docs/terrain-illumination-plan.md` built and that
Round 1's now-refuted "hard ceiling" theory was reasoning about).

**But there is a third possibility this formula's `ELEVATION_MOSAIC` branch is written for, and
it does NOT grow with overzoom at all: the texture-shading neighbor-ring mosaic
(`RasterSource::buildElevationMosaic`/`stitchElevationMosaic`).** Checked directly:

1. **Confirmed texture shading is ON in the current test session** — `~/.config/Ascend/config.yaml`
   has `texture_shading: { enabled: true }`, and `MapsApp` (`app/src/mapsapp.cpp:2467,795`) loads
   this into `textureShading` and pushes `SceneUpdate{"global.elevation_mosaic", "true"}` at scene
   load — overriding the scene's own default (`elevation_mosaic: false`, `elevation.yaml:12`).
   This is the exact same class of confound CLAUDE.md already warns about for
   `sources.last_source`/`show_trails` ("this exact confound previously masked a real
   peak-visibility bug for an entire multi-round testing effort before it was caught") — a
   persisted GUI toggle silently changing which code path renders, unnoticed across sessions.
2. With `global.elevation_mosaic` true, `ELEVATION_MOSAIC` is compiled in, and
   `RasterTileTask::addRaster()` (`rasterSource.cpp:545-643`) takes this branch for the vector
   tile's elevation attachment:
   ```cpp
   if (!source->m_buildElevationMosaic) {
       ... buildOverzoomElevationMosaic(...) ...   // SKIPPED ENTIRELY when texture shading is on
   }
   if (source->m_buildElevationMosaic) {
       mosaicTex = source->buildElevationMosaic(m_tileId, raster->texture);  // <-- this, instead
       ...
   }
   ```
   **The overzoom-composite path is gated behind `if (!source->m_buildElevationMosaic)` and never
   runs at all when texture shading is on** — this exact scoping decision is explicitly
   acknowledged in `docs/terrain-illumination-plan.md`'s "third phenomenon" section: *"the
   overzoom composite now only engages when `!source->m_buildElevationMosaic`; when texture
   shading is on, this pass's fix is a no-op ... Composing the two mechanisms is deferred to the
   future upgrade path below."* That deferred future work was never done.
3. `buildElevationMosaic`/`stitchElevationMosaic` builds a 3Wp×3Wp neighbor-ring mosaic (own tile's
   data in the center third, 8 same-zoom neighbors around it — the texture-shading Laplacian-pyramid
   mechanism from `docs/texture-shading-plan.md`) from the **plain, native z14 texture**
   (`raster->texture`, confirmed always the plain per-tile texture at that point in `addRaster()`)
   — **never from any higher-resolution composite or descendant data**, regardless of how
   overzoomed the view is.
4. So with texture shading on, `rasterPixelSize(ELEVATION_INDEX).x == 3 * Wp` always (`Wp = 256`,
   the native per-tile size), giving `effectiveWp = Wp = 256` **unconditionally** — and therefore
   `cells = 64 * 256/256 * 1.0 = 64`, **frozen at the non-overzoom value forever**, no matter how
   far past z14 the view has zoomed. `getTerrainMeshElevation()`'s bilinear reconstruction is
   permanently stuck sampling a 64-cell lattice (~37 m spacing across a typical z14 tile at Alpine
   latitudes) even when the elevation source has genuinely finer z15/z16 data cached and available
   — that finer data is simply never consulted for the drape's own elevation function while this
   mode is active.
5. `terrain-ground` (`RasterStyle`) is completely unaffected by any of this — it reads
   `getElevation()` on its own, directly-fetched primary tiles (never through
   `RasterTileTask::addRaster()`'s vector-tile-attachment branch at all), which is exactly why it
   has been independently confirmed correct at every stage of this whole investigation.

### Why this retroactively explains every prior round's evidence, not just Round 3's

- **Round 1's 4x resolution bump had zero effect**: `kOverzoomCompositeMaxLevels` and
  `terrain_grid` both only govern the overzoom-**composite** path (`buildOverzoomElevationMosaic`)
  — which is completely bypassed while texture shading is on. Bumping them changes nothing
  observable, exactly as reported.
- **Round 1's +1000 m nudge fixed the gap**: consistent with a genuinely under-resolved (not
  wrong-location) elevation reconstruction — a 64-cell lattice under real, moderately-varied Alpine
  relief undershoots a true peak by a real, non-trivial, but bounded amount; +1000 m safely clears
  it without needing to be exactly right.
- **Round 2's ancestor-fallback LOGD check found nothing**: correctly so — that mechanism was
  never the cause; `u_raster_offsets.z == 1.0` throughout, consistent with this finding (no crop
  offset is needed or involved here at all).
- **Round 3's contour-line comparison** (agree near the summit, diverge — not in parallel — moving
  away): consistent with a single, now-abnormally-large (~37 m instead of the intended ~10 m or
  finer) lattice cell's bilinear undershoot from a true, sharp, curved terrain profile — largest at
  a cell's interior, resynchronizing at the next lattice corner. A smoothly growing, non-linear
  (not simply offset-and-parallel) divergence over a span of a few 100 m contour lines is exactly
  what that within-cell undershoot looks like, especially if the visible comparison area only spans
  a fraction of one (now oversized) cell before the next corner would resync it.

### Recommended immediate, zero-code confirmation (before any fix)

**Turn texture shading OFF** (the in-app "Texture shading" toggle, or edit
`~/.config/Ascend/config.yaml`'s `texture_shading.enabled` to `false`) and re-run the exact same
red/black contour-line comparison at the same Matterhorn location. With texture shading off,
`RasterTileTask::addRaster()` takes the **already-working** overzoom-composite path (the one Round
1's resolution-ceiling investigation correctly traced, just aimed at a bug that turned out to live
one level upstream of the ceiling's actual reach), and `getTerrainMeshElevation()`'s `cells` should
scale correctly with overzoom again. If the waterline/divergence shrinks dramatically or disappears
with texture shading off, that's a clean, decisive confirmation with no rebuild needed at all.

## Candidate fix directions

### (a) Extend the overzoom-composite mechanism to also run when texture shading is on

This is exactly the "future upgrade path" `docs/terrain-illumination-plan.md`'s third-phenomenon
section already scoped out and deferred: *"extend the composite to also satisfy the
`ELEVATION_MOSAIC`/`elevationMosaicUV()` contract... by feeding it as the 'own' cell of a
neighbor-ring mosaic, once same-size same-layout output is worked out, so the two mechanisms can
compose instead of being mutually exclusive."* Concretely: build the overzoom composite first (as
already happens when texture shading is off), then feed *that* (instead of the plain per-tile
texture) as the "own" cell into `buildElevationMosaic`/`stitchElevationMosaic`'s neighbor-ring
construction, so the mosaic's center cell carries genuinely overzoom-aware resolution and
`effectiveWp`/`cells` scale correctly again, while texture shading's own Laplacian-pyramid
machinery (which needs the neighbor ring, not the composite) keeps working unchanged.

- **Costs:** this is real, non-trivial work in code that's already been through several rounds of
  subtle regressions (`docs/terrain-illumination-plan.md`'s three "white patches" phenomena) —
  the neighbor cells would also need to be overzoom-composites for internal consistency (or the
  mosaic's per-cell sizes become inconsistent, likely reintroducing exactly the kind of "T-junction"
  cracks `TileManager::upgradeAttachedRasters`'s neighbor-`effectiveS` logic was built to prevent).
  Also touches `getElevationAtLod()`'s own `maxLod`/mip-chain assumptions (`elevation.yaml`), which
  assume a fixed `Wp`-based mosaic layout — would need re-verification that texture-shading's own
  multi-scale sampling still behaves correctly once the center cell is a variable-resolution
  composite rather than a fixed-size plain tile.
- **Benefit:** directly fixes the confirmed root cause with texture shading and the overzoom drape
  fix both fully working together, rather than one disabling the other.
- **Risk:** medium-high, given the history of this exact code area.

### (b) Make `getTerrainMeshElevation()`'s resolution derivation independent of the bound texture's raw pixel layout

Instead of inferring `cells` indirectly from `rasterPixelSize`/`ELEVATION_TILE_PIXELS`/
`u_raster_offsets.z` (fragile: it silently breaks whenever a texture serving a different purpose,
like the neighbor-ring mosaic, is bound instead of the overzoom composite it was written to
expect), have the C++ side pass the *actual* intended overzoom level explicitly as its own
uniform (a small, cheap addition parallel to `u_raster_offsets`/`u_tile_origin`, since the C++ side
already knows this precisely from `TileID.s`/`.z` at `Style::setupTileShaderUniforms` time,
independent of which texture layout ends up bound).

- **Costs:** a genuine interface change (new uniform, plumbed through `style.cpp`/shader blocks);
  needs care that it stays correct across the ancestor-fallback case too (Round 2's now-inert but
  still-real mechanism) and the parent-proxy case.
- **Benefit:** removes the whole class of "cells derivation silently wrong because it's inferring
  resolution from texture dimensions that serve an unrelated purpose" bugs — this is the second
  time this general shape of fragility has surfaced (this round via the mosaic; a future feature
  that binds yet another differently-laid-out texture to the same slot would hit it again). More
  robust long-term, not just a point fix for this one case.
- **Risk:** medium — smaller shader-side blast radius than (a), but is a real interface/architecture
  change to code with a history of regressions; needs the same before/after screenshot discipline.

### (c) Scope/document the limitation rather than fix it now

Leave texture shading and the overzoom-aware drape as mutually exclusive (as they already,
deliberately are per the third-phenomenon doc), and simply make sure this interaction is
documented/visible where Sebastian would see it (e.g. a GUI note, or just this doc) so it isn't
re-discovered as a mystery in a future session. Zero engineering risk, but leaves the actual bug
(visible whenever texture shading is left on, which per the CLAUDE.md-documented persisted-config
history is easy to do unintentionally) unresolved.

## Recommendation (Sebastian's call, not a decision)

**First, the zero-code confirmation above (texture shading off, re-run the contour comparison)** —
essentially free, and would upgrade this from "well-evidenced, code-confirmed mechanism" to
"visually confirmed root cause" before committing to any code change. Contingent on that
confirming, my recommendation is **(a)**, since it directly closes the gap the "third phenomenon"
investigation explicitly identified and deferred, rather than leaving texture shading and correct
overzoom drape mutually exclusive indefinitely — but (b) is worth real consideration too if
Sebastian would rather harden the general mechanism than extend the specific composite/mosaic
composition, given this is the second time inferring resolution from texture layout has caused a
silent, hard-to-diagnose bug. This is a recommendation for sign-off, not a decision — please
confirm direction before any implementation begins.
