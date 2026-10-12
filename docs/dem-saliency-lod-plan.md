# Saliency-driven DEM resolution ("helicopter view") — spec & plan

**Status:** proposal, nothing implemented. Written 2026-10-10 after perf traces
144723/150114/150323 (see `docs/profiling-plan.md` for the capture workflow). Per this repo's
workflow this needs Sebastian's sign-off before code.

## 1. The UX requirement, stated precisely

Sebastian, verbatim intent: the map should feel like a helicopter ride. A user standing low
over terrain asks "which peaks do I see on the horizon?" and the answer must be rendered with
real-world shape fidelity, not "blurry rough tessellated boxy things". Concretely:

| # | Requirement | Consequence for resolution |
|---|---|---|
| R1 | Peaks on/near the **skyline** match real shapes very closely | silhouette error budget is the tightest in the whole frame, at any distance |
| R2 | **Exposed rock/cliff/snow/glacier** is high-res; forest may be "blurry" | tolerance depends on surface cover, not just geometry |
| R3 | Far mountains are high-res **to the extent they are visible** | error is measured in *screen pixels*, so occlusion/foreshortening/distance all count |
| R4 | **Flat terrain** (plains, valleys, Germany below the horizon) needs almost no DEM detail | data-driven: if coarse and fine surfaces agree, stay coarse |
| R5 | Rivers, lakes, big city names carry recognition in flat areas | these are vector/label concerns, *not* DEM; never spend DEM budget on them |
| R6 | Must stay smooth (budgeted), and must not pop/flicker | hysteresis + a global budget, not a single bias slider |

## 2. Why today's mechanism cannot express this

* One quadtree walk (`TileManager::updateTileSets`, `tileManager.cpp`) decides *every* source's
  zoom with the same predicate: `tileScreenArea < maxArea / lodAreaBias²`, with
  `View::getTileScreenArea()` scaled by `steepnessCorrection()` (`view.cpp`).
* The only knobs are `resolution_bias` (global per-source multiplier on that area test) and
  `overzoom_step`. A single scalar cannot say "refine this ridge, ignore that plain": raising it
  multiplies leaf count everywhere. Traces 150114 vs 150323 (same pan, different resolution
  settings; which is which is unconfirmed) differ ~190 vs ~64 visible tiles and 92 vs 54 ms
  GPU `Render`, and the extra tiles are mostly low-saliency far field.
* `steepnessCorrection` is the right *instinct* (slope matters) but: uncapped, max over four
  fixed sample points, measured on the tile not on pixels, blind to skyline and cover.
* DEM tile data (`elevation`, Esri WorldElevation3D, `max_zoom: 16`) and the terrain mesh
  (`RasterStyle`, `kTerrainMeshGridRes = 64` quads per tile) scale together with the tile's
  zoom. The *silhouette* of a far ridge is formed by the mesh vertices; the *shading* by the DEM
  texels sampled per-fragment (hillshade.yaml). "Boxy" far peaks are a mesh-density problem at
  the skyline, which a coarser DEM tile cannot fix and a finer DEM tile does not fix unless the
  mesh follows.

## 3. The formalization: screen-space error with saliency weights

Replace the area rule, **for the elevation source only**, with a per-node predicate

    refine(T)  <=  E(T) > eps(T)

computed at each quadtree node T (all quantities already available or cheap):

### 3.1 Geometric error E(T), in pixels

    E(T) = G(T) * P(dist(T))                 [pixels]
    P(d) = viewportHeightPx / (2 * d * tan(fovY/2))      (pixels per meter at distance d)
    G(T) = expected height deviation (meters) between this node's DEM and its children's

* `G` is *data-driven*, which delivers R4 for free: a flat valley has G ~ 0 and stops at a
  coarse level at any distance; a cliff has large G.
* We cannot read the children's texels before deciding to load them, so G is **predicted
  from the node's own data** (available once the node's tile is decoded): local relief
  (min/max, already in `ElevationManager::getMinMaxElev`), slope percentile (p90/p99), and
  second-difference (curvature/roughness) energy at the node's native scale. Terrain is
  statistically self-similar, so roughness at scale s predicts the residual at s/2 (a per-tile
  constant `G = k * roughness`, calibrated once offline against real children — see Phase 1).
  Stored as a 4-float "detail descriptor" per elevation tile, computed on the decode worker.
* Foreshortening is already inside the geometric formulation if error is measured *along the
  view-perpendicular* — replace `steepnessCorrection` by the pixel-weighted visible-slope term
  below rather than patching it.

### 3.2 Tolerance eps(T), in pixels (the saliency term)

    eps(T) = eps_0 / ( w_skyline(T) * w_cover(T) * w_peak(T) )          clamp [eps_min, eps_max]

| weight | meaning | source | suggested default |
|---|---|---|---|
| `w_skyline` | node contains silhouette pixels (terrain vs sky) | last frame's terrain depth (`renderTerrainDepth` already produces it; per-column topmost terrain row) intersected with the node's projected bounds | x4 (eps 0.5 px vs 2 px) |
| `w_cover` | exposed rock/snow/ice/scree vs vegetated | per-tile fraction from the `osm` landcover (same pre-pass pattern as `AnchorOccupancyGrid::build` in `tileBuilder.cpp`) and/or DEM slope > ~35 deg | rock x2, forest x0.5 |
| `w_peak` | contains a ranked named peak (prominence) | peak features already carry prominence (`Label::refinePriority`) | x2 within r of the summit |

`eps_0` ~ 2 px is the global quality dial that replaces `resolution_bias` for DEM.

### 3.3 Silhouette is special: mesh vs texture

Two independent resolutions per elevation node, chosen separately:

1. **Texture** (DEM texels / overzoom mosaic): drives shading and cliff normals.
2. **Mesh** (vertices per tile; `kTerrainMeshGridRes`): drives silhouette and parallax.
   Allow a per-node mesh level `m(T)` >= tile default; skyline nodes get a denser grid
   (instanced from the same `RasterStyle` quad mesh at 64/128/256) while interior nodes keep
   64. This is where "boxy mountains" are actually fixed.

### 3.4 Budget (R6)

Do not threshold independently per node. Run a **best-first refinement** over the quadtree
with priority `E(T)/eps(T)` (benefit) per estimated cost (texels + tile loads + vertex count)
and stop at a global budget: `maxNewTexels`/`maxLeafTiles` per frame, plus a time-sliced
controller that tightens/loosens `eps_0` from measured main-thread/GPU time. Properties:

* Under load the far, low-saliency field is shed first; skyline peaks are shed last.
* Hysteresis: a node refined last frame uses eps x0.8 (needs 20% *less* error to unrefine);
  all weight changes are low-pass filtered, so a panning skyline does not strobe LOD.
* Deterministic given (camera, data) apart from the filtered state.

### 3.5 What stays vector/label (R5)

Water, city labels, roads: unchanged `osm` LOD (and the separate perf work on distance
cutoff of labels/tiles). Decoupling means `osm` can use a *coarse* bias while DEM is fine
only where salient — directly attacking the measured cost (≈70% of tile-worker time, all of it
`osm` `applyStyling`, goes to z<=10 horizon tiles; trace 144723).

## 4. Phased plan (each phase independently shippable, each measured)

* **Phase 0 — see it.** Debug overlay: colour nodes by predicted E/eps, `lod_debug` counters
  (leaves by reason: skyline/cover/peak/budget). No behavior change.
* **Phase 1 — detail descriptor.** Compute `{relief, slope_p90, slope_p99, roughness}` per
  decoded DEM tile on the worker; unit tests; calibrate `k` offline (compare predicted vs.
  actual child deviation on a few alpine + plain tiles).
* **Phase 2 — error-driven refinement for `elevation` only**, no saliency weights
  (`eps` constant), budget off. Gate by a scene flag; compare leaf counts and look against the
  area rule in flat vs alpine views. Expect: plains coarser, cliffs finer, at equal cost.
* **Phase 3 — saliency weights** (cover, peak, then skyline).
* **Phase 4 — mesh density per node** for skyline nodes.
* **Phase 5 — budget controller + hysteresis;** remove `resolution_bias` for DEM from the GUI
  (keep for `osm`).

## 5. Open decisions for Sebastian

1. **Skyline source:** previous-frame terrain depth (cheap, 1-frame lag, already exists) vs a
   cheap analytic horizon from the DEM (no lag, more code). Recommendation: depth buffer.
2. **Cover source:** landcover polygons only (needs `osm` loaded for that tile — chicken-egg
   for far tiles) vs DEM slope proxy (always available). Recommendation: slope proxy first.
3. **Budget semantics:** fixed leaf budget (predictable look) vs frame-time controller
   (predictable speed). Recommendation: frame-time controller with a floor on skyline nodes.
4. **Data ceiling:** the Esri source's real native resolution varies by region; saliency can
   only ask for detail the source has. Worth checking what we actually have over the Alps
   before promising "photorealistic".

## 6. Risks

* The roughness predictor can under-predict isolated spires (a single narrow peak in a smooth
  tile). Mitigation: weight by `w_peak` and keep the existing relief term as a floor.
* Skyline feedback loop (refining changes the skyline): filtered + hysteresis + one-frame lag.
* Mesh/texture mismatch cracks between nodes of different mesh level: needs skirt or
  edge-stitching (the existing terrain mesh already handles neighbour LOD differences for
  overzoom — verify before Phase 4).

## 7. Phase 1 calibration results (2026-10-10, offline, cached tiles)

Method: decode cached Esri WorldElevation3D tiles (LERC, 257x257 vertex grid) with the repo's
own LercLib; for every parent tile with all four children cached, compare each child to the
node-aligned bilinear upsample of its parent quadrant (what a coarser level renders) and
correlate the residual with statistics computed from the *parent only*.
~7.7k parent->child quadrants: 2.6k Alps (Matterhorn region, z9-z15) + 5.1k elsewhere
(Europe far field z8-z12, BC, Alps outliers).

| finding | numbers |
|---|---|
| **Data ceiling is not binding here** | new detail still appears z15->z16 in ~82% of quadrants (3.3 -> 1.7 m texels); z9-z14: ~100% |
| **Predictor** `lapRms` (RMS 4-neighbour Laplacian of the tile, meters) | `err_p99 ~ 1.0-1.1 x lapRms`; Spearman 0.86-0.95 within slope bins; log-linear R^2 0.84; 1-sigma spread x1.6 |
| Generalizes across regions | e/lap median 0.90-1.09 in every slope bin 10-90 deg, both samples |
| Beats the alternatives | relief rho 0.33, slope p90/p99 rho 0.6, lapRms 0.65 pooled-per-z (0.86-0.95 per slope bin) |
| **Flat terrain is cheap** | slope <10 deg: median child error 0.3-1.3 m vs ~5-11 m on 30-90 deg faces |
| **Known failure mode** | ~17% of quadrants have error >3x predicted: smooth-at-parent tiles (median lapRms 1.3 m) whose children contain new small-scale structure. Absolute error there is small (~4 m) but it is the "smooth coarse tile hides a spire" case; mitigate with a relief-based floor + peak boost (w_peak), not by trusting lapRms alone |

Caveats: the sample is dominated by alpine terrain (few truly flat parents, 99 quadrants <3 deg);
the cache is biased toward places Sebastian has viewed; error is measured in height, not yet
in screen pixels / silhouette displacement.

Implemented from this: `ElevationManager::getTerrainDetail()` (lapRms/relief cached per elevation
texture) and capture-time `lodElev*` counters in `Scene::update` comparing the current area
rule's leaves against predicted screen-space error (diagnostic only; no LOD behavior change).

## 8. Findings from the 4x vs 1x captures (063843 / 063925) and the revised criterion

* **4x is uniform and wasteful.** 123 elevation leaves vs 39 at 1x; ~82 of the 123 have
  predicted near-edge error < 0.5 px; 21.5/frame are refinements whose parent was already
  < 0.5 px. Cost: 95 vs 66 ms mean frame, 811 vs 236 draw calls.
* **4x does not help the closest mountains because they are capped, not area-limited.** At 4x,
  ~70 elevation leaves/frame stop by `stoppedByMaxZoom` at exactly z14
  (`viewZoomCap`, `tileManager.cpp`: `max(getIntegerLodZoom, int(getTileDistanceZoomBound))`,
  evaluated at the tile *center* at sea level and truncated with `int()`). The area-rule bias
  cannot push past that cap; it only helps mid-distance tiles, matching the observed "helps
  far-ish mountains, subtly".
* **Height error in pixels is the wrong saliency for shaded relief.** A cliff face has a small
  height error but a maximal *normal* error; shading, not displacement, is what reads as
  detail. Revised criterion: refine node T iff
  (a) child texel >= pixel footprint at T's nearest point / 2^boost (Nyquist; nothing finer can
  be shown), and (b) slope change per texel `lapRms / texelSize >= tau` (detail exists, in
  slope terms, scale-free). Implemented as `View::demSaliencyWantsChildren` +
  `TileManager` override (env `ASCEND_DEM_LOD=1`, `ASCEND_DEM_TAU` default 0.07,
  `ASCEND_DEM_BOOST` default 0.5, `ASCEND_DEM_LEAF_BUDGET` default 160). Union with the area
  rule: it only ever ADDS refinement.
* **Pixel arithmetic limits "z16 everywhere".** Ground pixel footprint = d / P. At zoom 12
  tilted, nearest terrain is a few km away: footprint ~3-8 m, i.e. z14-z15 texels (6.6/3.3 m).
  z16 (1.7 m) only improves *displayed* detail within ~2-3 km, or via oversampling (`boost`)
  that helps normals/aliasing but not geometry.
* **Silhouette ("boxy") is the terrain MESH**: 64x64 quads per tile means a z14 tile has a vertex
  every 4 texels (~26 m); ridgelines are formed by those vertices while shading uses texels.
  A denser mesh is NOT a one-constant change: the lattice size 64 is hard-coded in
  `elevation.yaml` `getTerrainMeshElevation` (`cells = 64.0 * ...`), in
  `rasterSource.cpp` (lines ~176-197 lattice sampling / triangle-PL upsampling),
  `RasterStyle::build`, and `PolygonStyleBuilder::setup`'s grid. All must change together
  (single shader define + C++ constant) -> Phase 4, deliberately not hacked in as an env knob.
