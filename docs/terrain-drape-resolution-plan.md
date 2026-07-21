# Terrain-drape resolution ceiling — investigation & fix options

> **SUPERSEDED (2026-07-19).** The "hard resolution ceiling" conclusion below was Sebastian's
> and the team's next hypothesis to test, and it was **refuted by direct experiment**: a 4x
> resolution bump (elevation `max_zoom` 16→18, `kOverzoomCompositeMaxLevels` 2→4, `terrain_grid`
> 256→1024, full rebuild, since fully reverted) produced **zero visible change** to the reported
> "waterline" bug. Further empirical diagnostics (recoloring terrain-ground vs. `bare_rock`
> separately, a constant-elevation nudge test, and a live visualization of
> `u_raster_offsets[ELEVATION_INDEX].z`) found the actual mechanism: the elevation raster
> **attached to specific draped vector tiles** near the summit is getting **persistently pinned
> to a coarser ancestor tile** (not a resolution/tessellation-density problem at all, but a
> **wrong-VALUE problem** — a coarse, spatially-averaged ancestor genuinely reads the peak height
> far too low). See **`docs/terrain-drape-elevation-attachment-plan.md`** for the corrected,
> evidence-based investigation, confirmed root cause, and candidate fixes. This document is kept
> for its code-citation legwork (much of which remains accurate and relevant — e.g. the `gridRes`
> reactivity findings in items 1-2 below, the `TileID`/`TileCache` mechanics, and the ruled-out
> items in item 6) but its headline conclusion (item 3/4: "both cap at the same ceiling, and
> that's the bug") is **not** the actual cause of the reported symptom and should not be acted on.

---

# Terrain-drape resolution ceiling — investigation & fix options

**Status:** research complete, no fixes implemented. Written per Sebastian's "research first,
present plan, implement only after sign-off" workflow (same process used for the hillshade GPU
investigation and the "white patches" rounds in `docs/terrain-illumination-plan.md`). Do not
implement any of the candidate fixes below without explicit approval.

## The report

Testing the new `rock-texture` style (`styles.rock-texture`, mixed into `unlit-polygons`,
applied only to `natural: bare_rock` land-cover polygons — `assets/scenes/stylus-osm.yaml`)
over the Matterhorn (45.9763, 7.6586), Sebastian found that near the summit, in 3D terrain
mode, the `bare_rock` polygon fill simply stops covering the ground above a certain point —
confirmed by temporarily recoloring `bare_rock` bright blue: far from the peak the mountain is
blue, but above a "waterline" (not a contour line) it reverts to background-colored, which
happens to look like a plausible warm-gray rock color once hillshade is blended over it (which
is why it wasn't obvious before the blue diagnostic). The waterline **recedes further down the
mountain as you zoom in**, and **recedes further still as the camera tilts from zenithal toward
grazing**.

This is distinct from the previously-fixed "white patches" family in
`docs/terrain-illumination-plan.md` (proxy-mosaic remap, Lambertian clipping on cliffs, and the
raster-zoom mismatch) — this is a drape *tessellation* mismatch, and apparently much larger in
extent than the "~1% at cols" residual the earlier rounds flagged as acceptable, likely because
those investigations used subtle diagnostic colors that masked it while the bright-blue test used
here does not.

## The hypothesis handed to this investigation

That `PolygonStyleBuilder::setup()` (`tangram-es/core/src/style/polygonStyle.cpp`) computes the
polygon drape mesh's tessellation density (`gridRes`) exactly once, when a tile's raw data is
first built into a mesh — and that this build never re-runs as the user continues zooming in
within the same, already-built `(x,y,z)` tile footprint, because the `osm` vector source caps at
`max_zoom: 14`. If true, the drape mesh's own vertex density would be frozen at whatever the view
zoom happened to be at build time, while the *reference* elevation surface it drapes onto
(`getTerrainMeshElevation()`, `assets/scenes/elevation.yaml`) keeps getting finer forever as the
elevation raster attachment upgrades — producing an ever-widening gap.

**This hypothesis is refuted in its strongest form (permanently frozen), but a related, more
precise mechanism is confirmed and is very likely the actual cause.** Details below.

## What's actually confirmed, with evidence

### 1. `gridRes` is NOT frozen forever — tiles do rebuild as `s` climbs

`TileID` (`tangram-es/core/include/tangram/tile/tileID.h`) carries both `z` (data zoom) and `s`
(styling/display zoom). `TileManager::updateTileSets` → `getVisibleTiles`
(`tangram-es/core/src/tile/tileManager.cpp:266-334`) computes, for a tile whose data source has
hit its own `max_zoom` (`stoppedByMaxZoom`, e.g. `osm` at 14):

```cpp
float stepExponent = tileSet.source->overzoomStepExponent();  // default 2.0
int s = tileId.z + std::max(0, int(std::ceil(std::log2(area/effMaxArea)/stepExponent)));
visId.s = std::max(std::min(s, _view.getIntegerZoom()), visId.z + zoomBias);
```

`s` climbs roughly 1 per view-zoom-level once overzoomed (area quadruples per zoom level,
`stepExponent` divides `log2` by 2). `TileCache` (`tangram-es/core/src/tile/tileCache.h`) keys
entries by `(sourceId, TileID)` **including `s`** (`TileCacheKey = std::pair<int32_t, TileID>`,
`put()`/`get()` use `tile->getID()`/the passed `TileID` verbatim). `TileManager::updateTileSet`'s
diff loop (`tileManager.cpp:448-537`) treats a `visTileId` whose `s` no longer matches any current
map entry as "missing," and `addTile()` (`tileManager.cpp:1163`) does a **fresh cache lookup keyed
on the new `s`** — a miss there creates a brand-new `TileTask` (`entry.task =
_tileSet.source->createTask(_tileID)`), which runs the full `TileBuilder::build()` pipeline again,
including `PolygonStyleBuilder::setup()` with the new `id.s`:

```cpp
// polygonStyle.cpp:124-127
int overzoomLevels = std::max(0, int(id.s) - int(id.z));
m_builder.gridRes = m_style.activeTerrainGrid() > 0
    ? std::min(m_style.activeTerrainGrid(), kTerrainMeshGridRes << overzoomLevels)
    : 0;
```

So continuing to zoom in on an already-loaded tile **does** eventually trigger real rebuilds with
a larger `gridRes`, roughly once per view-zoom-level while `s` is climbing — not never. (This
CPU cost is real and already flagged in `docs/tile-pipeline-perf-plan.md`'s R1 — see the GPU-cost
discussion below — but it isn't zero, and it isn't "frozen forever.")

### 2. The elevation raster attachment for vector tiles genuinely does upgrade continuously — confirmed, and now more sophisticated than the "third phenomenon" doc described

`TileManager::upgradeAttachedRasters` (`tileManager.cpp:878-1161`) runs from `updateTileSet` for
**every visible tile, every `updateTileSets()` call** (i.e. every frame the view is dirty), not
just when a tile is rebuilt. For draped vector tiles (`osm`) it recomputes an `effectiveS` (the
max of this tile's own live `s` and its same-zoom neighbors' `s`, to avoid T-junction cracks at
tile borders — see the "attachment lifecycle" comment at `tileManager.cpp:915-938`), then calls
`RasterSource::buildOverzoomElevationMosaic(livePrimary, plainBase, caps)`
(`tangram-es/core/src/data/rasterSource.cpp:1032`), which **always builds directly at the full
target resolution** `zt = overzoomTargetZoom(livePrimary)`, filling any not-yet-cached cell by
upsampling a coarser ancestor rather than skipping the upgrade. This is a materially more complete
mechanism than `docs/terrain-illumination-plan.md`'s "third phenomenon" section describes (which
called the active-fetch/full-coverage version a "future upgrade path, not implemented") — that
work has since landed. Confirmed this applies to vector (`osm`) tiles' elevation attachment, not
just the raster (`terrain-ground`) tiles' own attachment (Case 2 vs. Case 1 in
`upgradeAttachedRasters`).

### 3. Both `gridRes` and the reference lattice are capped at the SAME absolute resolution, by deliberate, coordinated design — and that cap is a hard data-availability ceiling, not an accidental drift

`RasterSource::overzoomTargetZoom` (`rasterSource.cpp:1000-1009`):

```cpp
int zt = std::min<int>(m_zoomOptions.maxZoom, _primary.s);
zt = std::min<int>(zt, _primary.z + kOverzoomCompositeMaxLevels);  // kOverzoomCompositeMaxLevels = 2
```

For the `osm`/`elevation` pairing (`osm` `max_zoom: 14`, `elevation` `max_zoom: 16`,
`assets/scenes/stylus-osm.yaml` / `elevation.yaml`), `zt` caps at `min(16, s)`, itself capped at
`14 + 2 = 16` — i.e. **`zt` never exceeds 16, no matter how far `s` climbs past that.** The
elevation raster attachment's own pixel resolution (`effectiveWp`) is therefore fixed once
`zt` = 16 is reached, and the reference lattice cell count evaluated in
`getTerrainMeshElevation()` (`elevation.yaml`):

```glsl
float cells = 64.0 * effectiveWp / float(ELEVATION_TILE_PIXELS) * u_raster_offsets[ELEVATION_INDEX].z;
```

evaluates to a fixed `64 * 2^(zt-14) = 64*4 = 256` once `zt` caps at 16 — **and freezes there for
any further zoom-in.**

Independently, the polygon's own `gridRes` formula (`polygonStyle.cpp:125-126`, quoted above) is
`min(terrain_grid ceiling, 64 << overzoomLevels)`. The style's ceiling is set in
`assets/scenes/stylus-osm.yaml`:

```yaml
# ... "osm" source caps at max_zoom 14 ... while the elevation source ... goes to
# 16, so tiles are overzoomed up to 2 levels and the lattice is up to 64 * 2^2 = 256 cells per
# tile; 256 matches that worst case (power-of-two multiples of the lattice nest exactly, so it's
# also correct at lower view zooms).
unlit-polygons:
  ...
  terrain_grid: 256
```

**This is not a coincidence — `terrain_grid: 256` was deliberately chosen to equal
`kTerrainMeshGridRes(64) << kOverzoomCompositeMaxLevels(2)`.** Both `gridRes` and the reference
lattice cap at exactly the same absolute value (256 cells across the vector tile's own ~z14
footprint) once display zoom reaches roughly z16-equivalent for this tile — by design, confirmed
directly in the code comment above, which explicitly reasons about correctness "at lower view
zooms" (i.e. up to the ceiling) but does not claim (and the code does not provide) any behavior
*past* it.

**So the two quantities the hypothesis worried about drifting apart are, in fact, kept in
lockstep by construction, up to a shared ceiling.** The premise "gridRes is frozen while the
reference keeps sharpening indefinitely" does not hold: both freeze at the same value, at
(approximately) the same time, for the same underlying reason — there is no finer elevation DEM
data past `elevation`'s own `max_zoom: 16`.

### 4. The actual mechanism: a hard, fixed-resolution ceiling becomes more visible the more you zoom/tilt past it

Once `gridRes` and the reference lattice both cap at 256 cells across a ~z14 tile footprint
(roughly ~9-10 m per cell at Alpine latitudes), that is the **best achievable geometric
resolution for the drape, permanently** — not because of any bug, but because there is no finer
DEM data to represent a finer surface with. A piecewise-linear mesh mathematically must sit
at-or-below a true convex peak strictly between lattice vertices; the residual gap at a sharp
summit is therefore a **fixed, real-world-sized error** once the ceiling is reached. Continuing to
zoom in past that point doesn't make the mesh relatively coarser — it just keeps magnifying a
gap that was always geometrically present but too small on screen to be noticeable before. Tilting
toward grazing amplifies the same fixed real-world (mostly lateral, here) error into a much larger
apparent screen-space region, exactly the parallax-amplification mechanism already documented for
the distinct raster-zoom-mismatch bug in `docs/terrain-illumination-plan.md`'s "third phenomenon."

This matches both reported symptoms precisely: recedes further on zoom-in (you are zooming into a
now-fixed, un-improvable approximation), and recedes further under grazing tilt (parallax
amplification of the same fixed error). It is also fully consistent with the bug appearing exactly
**at the summit** — the point of highest true curvature, where a fixed-resolution piecewise-linear
mesh's undershoot is unavoidably largest.

**Where the polygon fill actually disappears** (rather than merely looking slightly displaced):
the terrain-ground/hillshade mesh (`RasterStyle::build`, `rasterStyle.cpp`) places its own mesh
vertices *directly* at its fixed 64-cell lattice and samples elevation there with a plain
`getElevation()` texture read (`terrain-3d.yaml:65`) — that mesh IS the ground, opaque,
"solid earth" for rendering purposes. The `bare_rock` polygon is a separate, later-drawn overlay
whose own vertices are placed via `getTerrainMeshElevation()` to try to sit exactly on that same
surface (`terrain-3d.yaml:70`). Once the polygon's own tessellation is *coarser* than what would
be needed to track a locally sharp fold in that reference surface (which happens increasingly
often as both cap out and the true summit keeps getting visually magnified), the polygon's
interpolated vertices sag measurably below the terrain-ground mesh's own surface at the same
point — and the opaque terrain-ground geometry (rendered first / at same depth) wins the
z-test/occludes the polygon there, so the fill silently disappears rather than merely looking
subtly wrong. This is the same class of failure as the original "white patches" bug (terrain
poking through where the drape dips below it), just triggered by the fixed-resolution ceiling
rather than a data-freshness or raster-zoom bug.

### 5. A secondary, smaller, currently-uncorrected mechanism: cross-tile neighbor bumping is one-sided

`upgradeAttachedRasters`'s neighbor-aware `effectiveS` (item 2 above) deliberately bumps a tile's
**reference lattice** (`cells`) up to match a finer same-zoom neighbor's `s`, specifically to
avoid a T-junction crack at the shared edge (comment at `tileManager.cpp:915-938`). There is **no
corresponding neighbor-awareness in `PolygonStyleBuilder::setup()`** — `gridRes` is computed from
`id.s` alone, with zero knowledge of neighboring tiles. A tile with a lower `s` than a neighbor
(common under tilt, where near/far tiles can differ) gets its *reference* bumped to match the
neighbor but its *own mesh density* does not follow — i.e. `cells > gridRes` can occur locally,
near tile borders, independent of the global 256 ceiling in item 3. This is real but narrower in
scope (near-boundary only, not a broad summit-area effect), so it's very unlikely to be the
primary driver of the reported symptom, but it is a second source of the same undershoot class and
should be tracked separately.

### 6. Ruled out

- The vertex-budget/truncation issue from the second "white patches" round
  (`docs/terrain-illumination-plan.md`) is not the mechanism here: `buildPolygonGrid`
  (`tangram-es/core/src/util/builders.cpp:107-268`) now flushes an over-budget feature into a new
  indexed chunk (`chunkOffsets`, `flushChunkIfNeeded()`) rather than falling back to an undraped
  fallback triangle — full drape coverage is preserved regardless of polygon size/complexity, just
  possibly split across multiple index sub-ranges. No coverage loss from this path currently.
- `RasterStyle`'s own mesh resolution (`rasterStyle.cpp:62`, `kTerrainMeshGridRes` fixed at 64
  regardless of overzoom) is unrelated to `gridRes` drift — it's a hard-coded constant by design,
  and (per item 4) it IS what the vector polygon's reference lattice is built to reproduce exactly
  when not overzoomed, and to extrapolate via the shared overzoom-composite mechanism when it is.

## Summary of confirmed root cause

The `bare_rock` (and, more generally, any `terrain_grid`-tessellated polygon's) drape resolution
is capped at a **hard, deliberately-chosen ceiling** tied to the gap between the `osm` vector
source's `max_zoom: 14` and the `elevation` raster source's `max_zoom: 16`
(`kOverzoomCompositeMaxLevels = 2`, `terrain_grid: 256`) — both the polygon mesh's own
tessellation density and the reference surface it drapes onto are, by design, frozen at that same
ceiling once display zoom reaches roughly z16-equivalent for a z14 vector tile. That ceiling was
sized correctly for "no worse than the true achievable DEM resolution" but was evidently never
stress-tested at the very high zoom / grazing-tilt combinations Sebastian used while chasing the
`rock-texture` diagnostic — at those extremes, the fixed, now-un-improvable piecewise-linear
approximation of the true (sharp) summit becomes large enough on screen that the terrain-ground
mesh visibly wins the z-test over the drape polygon near the peak, producing the reported
"waterline." A secondary, narrower, currently-real mismatch (item 5) can independently produce the
same class of artifact right at tile borders when neighboring tiles disagree on `s`.

## Candidate fix directions

### (a) Raise the ceiling: bump `terrain_grid` and `kOverzoomCompositeMaxLevels` together

Increase `kOverzoomCompositeMaxLevels` (currently 2, `rasterSource.cpp:286`) to 3 or 4, and raise
`unlit-polygons`' `terrain_grid: 256` to match (`64 << 3 = 512`, `64 << 4 = 1024`), keeping the two
in lockstep exactly as they are today. This pushes the "you've zoomed in far enough to see the
facets" point further out without changing the architecture at all — it's a pure resolution bump
on both sides of an already-correctly-synchronized pair.

- **Costs:** no new real DEM detail is created (elevation's own native data still tops out at
  z16) — this only lets the *interpolation* go finer, so it delays rather than eliminates the
  underlying limit; the true summit is still, in principle, arbitrarily sharper than any finite
  mesh. More importantly: `docs/tile-pipeline-perf-plan.md`'s R1 already measured
  `buildPolygonGrid` at 26-49% of all vector-tile build CPU with the *current* 256 ceiling
  (10.4x triangle amplification, worst single polygon 33.8 ms release / 907 ms debug), and — more
  urgently — `unlit-polygons` is one of only two styles (`hillshade` and `unlit-polygons`, ~80% of
  all GPU frame time combined per the just-completed profiling investigation, see
  `docs/profiling-plan.md`/profiling-system-state memory) that individually exceed the entire 60fps
  frame budget. Raising resolution further directly worsens a cost that is *already flagged as the
  single biggest lever in active profiling work*, with no compensating win outside the narrow
  extreme-zoom/grazing-tilt scenario this bug lives in. This is the "raising is more palatable than
  lowering was" case flagged in the task brief (an earlier adaptive-*lowering* idea was explicitly
  skipped "per Sebastian's call: fidelity over that CPU saving") — but the calculus has changed
  since that call was made, given the concurrent GPU-cost finding; this tradeoff should be weighed
  with that context in view, not in isolation.
- **Risk:** low-to-medium — same code paths as today, just larger numbers; needs a fresh CPU/GPU
  cost measurement (tile-pipeline-perf-plan's methodology) at the new ceiling before committing.

### (b) Adaptive, curvature-triggered local refinement instead of a uniform ceiling bump

Keep the 256 ceiling as the default, but detect high local curvature (a cheap proxy already
exists: `computeRugosity`/texture-shading's Laplacian pyramid machinery, or simply comparing
`getTerrainMeshElevation()`'s bilinear estimate against a directly-sampled finer probe) and only
refine the grid locally where curvature is high (i.e., near true summits/ridgelines) rather than
uniformly across every tile. This targets the actual failure mode (sharp convex peaks) instead of
paying the cost everywhere.

- **Costs:** meaningfully more implementation complexity than (a) — `buildPolygonGrid`'s current
  design assumes a single, uniform `gridRes` per tile (its clip-based cell tessellation, corner
  sharing, and the terrain-mesh diagonal-matching guarantee all lean on that uniformity); adaptive
  local density would need either a quadtree-like recursive subdivision per cell or a
  post-tessellation "insert extra vertices near flagged cells" pass, both of which risk
  reintroducing T-junction cracks (the exact class of bug items 2/5 above were built to avoid) at
  the boundary between refined and unrefined regions.
- **Benefit:** avoids uniformly taxing GPU/CPU cost across all terrain (most land-cover polygons
  are nowhere near a sharp summit), so it could improve the specific bug without worsening the
  already-flagged `unlit-polygons` GPU cost broadly — cost is paid only where curvature actually
  demands it.
- **Risk:** medium-high. This is exactly the kind of change the terrain-drape code's history (three
  rounds of "white patches" fixes) suggests deserves the most caution — new geometric edge cases at
  refinement boundaries are plausible, and testing them thoroughly (multiple alpine locations,
  multiple zoom/tilt combinations) would take real effort.

### (c) Fix it on the terrain-ground/raster-mesh side instead of the polygon side

Since the actual failure mode (item 4) is that the *opaque terrain-ground mesh* wins the z-test
over the drape polygon near a sharp peak, an alternative is to stop that specific occlusion instead
of chasing polygon resolution: e.g. draw `terrain_grid`-tessellated polygons with a depth bias/test
override that reliably wins over terrain-ground regardless of small vertex-height discrepancies
(the existing `depth_shift = -0.02*u_proj[2][3]` in `terrain-3d.yaml:57` already does something in
this spirit for other cases), or increase `RasterStyle`'s own mesh resolution
(`kTerrainMeshGridRes`, currently a hard-coded 64 for terrain-ground/hillshade regardless of
overzoom) so the *reference* surface itself is finer, independent of the vector-polygon ceiling
question entirely.

- **Costs:** a depth-bias-only fix could mask the symptom (fill no longer disappears) while leaving
  the underlying geometric mismatch (and its visual "wobble"/shape inaccuracy right at the summit)
  unaddressed — polygons might now render fully but visibly not match the true terrain silhouette,
  which may or may not be an acceptable tradeoff depending on how it looks in practice (a call for
  Sebastian, not something to guess at from code alone). Raising `kTerrainMeshGridRes` itself would
  add cost to *every* 3D terrain tile (raster + drape both), a broader and less targeted change than
  (a) or (b).
- **Risk:** low for the depth-bias variant (small, contained shader change); the terrain-illumination
  history (`docs/terrain-illumination-plan.md`) shows this exact kind of code has produced surprising
  regressions before (the raster-mesh-resolution-vs-2D-quad diff noted in that doc's "Zenith
  continuity" validation), so any change here needs the same before/after screenshot discipline that
  document used.

## Recommendation (Sebastian's call, not a decision)

Given the concurrent, unresolved finding that `unlit-polygons` is already one of the two dominant
GPU-time consumers in the app (per the profiling investigation currently in progress), a blanket
resolution increase (a) should not be adopted casually even though it is the simplest and
lowest-risk option — it directly worsens a cost that a parallel investigation is actively trying to
bring down. My inclination is that (b) (curvature-triggered local refinement) is the more
principled fix in spirit — it targets exactly the geometric situation that causes the bug (sharp
convex peaks) without a blanket cost increase — but it is also the most complex and highest-risk
option to implement correctly given this code's specific history of edge-case regressions. A
smaller-scope version of (a) — a modest, one-notch ceiling bump (e.g. 256→384 or 512, with
`kOverzoomCompositeMaxLevels` 2→3) paired with a real before/after CPU/GPU measurement at the exact
zoom/tilt combination that reproduced this bug — might be the pragmatic first move to quantify how
much of the visible gap that alone buys back, before committing to the larger complexity of (b) or
investing further in the profiling-side hillshade/unlit-polygons investigation that's already
running in parallel. This is a recommendation for sign-off, not a decision — please confirm
direction (or propose something else) before any implementation begins.
