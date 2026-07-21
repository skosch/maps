# Terrain-drape `cellCaps` investigation — the composite's target zoom is capped by the vector tile's `s`, which can sit BELOW the terrain's actually-displayed zoom

**Status (2026-07-19, round 8):** research only, no fixes implemented, per the established
"research first, present plan, implement only after sign-off" workflow. This round was asked to
trace whether the `cellCaps` displayed-terrain-zoom-cap mechanism in
`TileManager::upgradeAttachedRasters` lands on a too-low zoom near the Matterhorn summit. The
line-by-line trace is below. The headline finding is a refinement of that hypothesis, not a
confirmation of it as stated: **the caps themselves can only clamp the composite DOWN toward
coarser zooms and appear correctly built; the actual hole is one level up — the composite's
overall target zoom `zt` is hard-capped by the draped vector tile's own styling zoom `s`
(`RasterSource::overzoomTargetZoom`, `rasterSource.cpp:1005-1014`), and `s` is computed by a
whole-tile aggregate-screen-area heuristic that systematically underestimates the per-quadrant
subdivision depth the elevation tileset's own recursive traversal reaches in the
screen-area-concentrated (near / focused-on) part of a tilted view. Cells whose displayed
terrain is FINER than `zt` fall into the cap loop's `zTarget < 0` "leave at zt" branch
(`tileManager.cpp:1113`) and the drape is then built from data one — after weak-cache eviction
of intermediate zooms, up to two — levels coarser than the terrain rendered beneath it.**
None of the three constants raised in round 7 (`kOverzoomCompositeMaxLevels`, elevation
`max_zoom`, `terrain_grid`) touches `s`, which is exactly why that experiment was a clean
no-effect: `s` was the binding term of the `min()` the whole time.

This is presented as the single strongest candidate, **not** as a confirmed root cause: it makes
a sharp, falsifiable prediction (§6) and has a near-trivial log diagnostic (§7.1) that will
confirm or kill it in one session. If the trusted contour dataset (confirmed fact 8 of the
current round summary) was collected at *exactly* `--view.tilt 0`, this hypothesis alone cannot
explain that dataset and the fallback candidates in §8 take over — that question is itself part
of diagnostic §7.0.

---

## 1. What was traced, with citations

All file paths relative to the repo root; `tileManager.cpp` = `tangram-es/core/src/tile/tileManager.cpp`,
`rasterSource.cpp` = `tangram-es/core/src/data/rasterSource.cpp`. Line numbers are from the
live tree as of 2026-07-19 (with the round-7 TEMP experiment constants still in place).

### 1.1 The `cellCaps` computation (`tileManager.cpp:1007-1137`)

For every visible draped vector tile, every `updateTileSets()` pass (`tileManager.cpp:476` and
`:1174` are the only call sites of `upgradeAttachedRasters`), the code:

1. Computes a neighbor-aware effective styling zoom (`effectiveS`, `:939-964`): the max of this
   tile's live `s` and the `s` of its 8 same-zoom neighbors *in the vector tileset's own
   `visibleTiles`*. Note this only consults **vector** neighbors' `s`; it never consults the
   elevation tileset's displayed zoom.
2. Builds `livePrimary = (x, y, z=14, s=max(effectiveS, z))` (`:978-979`) and the composite
   target `zt = rs->overzoomTargetZoom(livePrimary)` (`:1017`), where
   (`rasterSource.cpp:1005-1014`):

   ```cpp
   int zt = std::min<int>(m_zoomOptions.maxZoom, _primary.s);
   zt = std::min<int>(zt, _primary.z + kOverzoomCompositeMaxLevels);
   ```

   **`zt` can never exceed `s`.** This is the load-bearing line for this round's finding.
3. Builds two normalized `(x,y,z)` sets from the elevation tileset (`:1050-1092`):
   `rendered` = `elevSet->visibleTiles` (the traversal's steady-state display target), `ready` =
   entries of `elevSet->tiles` that hold a built `Tile`. The `s`-normalization subtlety here was
   already found and fixed in an earlier round (comment at `:1073-1085`, the "mottled salmon
   patches" fix); the sets are like-for-like now.
4. Fills `cellCaps` (`:1098-1137`): for each of the N×N zt-grid cells, walks
   `for (int za = zt; za >= 0; za--)` (`:1106`) looking for the finest `rendered` tile covering
   the cell, then checks that tile's readiness and, if not ready, walks to the nearest ready
   **ancestor** down to `livePrimary.z` (`:1118-1131`).
5. `buildOverzoomElevationMosaic` consumes the caps via `cellTargetZoom`
   (`rasterSource.cpp:1055-1059`):

   ```cpp
   int cap = int(_cellZoomCaps[size_t(row) * size_t(N) + size_t(col)]);
   return std::max<int>(_primary.z, std::min<int>(zt, cap));
   ```

   **A cap can only lower a cell below `zt`; a cap above `zt` is clipped to `zt`.** The cells
   are initialized to `zt` (`tileManager.cpp:1099`) and the `zTarget < 0` branch (`:1113`,
   `// no target covers this cell; leave at zt`) leaves them there.

### 1.2 The critical blind spot in the cap walk

The walk at `:1106` starts **at `zt` and only goes coarser**. `TileSet::visibleTiles` contains
only traversal *leaves* (`tileManager.cpp:310-327`: a tile is inserted exactly when its tileset
stops subdividing, and subdivision recurses only for still-active tilesets). So if the elevation
traversal subdivided *past* `zt` under some cell — i.e. the terrain there is being **displayed
finer than the drape's ceiling** — then:

- the displayed z16 leaf is at `za > zt` and is never tested by the walk;
- its z15/z14 ancestors were subdivided *through*, so they are not leaves and not in
  `rendered` either;
- the walk finds nothing, `zTarget` stays `-1`, and the cell silently keeps the `zt` default.

There is no log, no warning, and no mechanism anywhere in `upgradeAttachedRasters` or
`buildOverzoomElevationMosaic` that can push a cell (or `zt` itself) *up* to match
finer-displayed terrain. The entire mechanism is one-directional by construction, and the code's
own doc comments (`:1007-1016`) only ever contemplate the "terrain displays coarser than the
composite could be" direction — the opposite direction was evidently assumed impossible.

### 1.3 Why displayed terrain CAN be finer than `zt`: the two LOD heuristics are different algorithms

Both decisions live in `TileManager::updateTileSets`' `getVisibleTiles` recursion
(`tileManager.cpp:266-334`) and share `View::getTileScreenArea` (`view.cpp:614-674`), but they
use the area differently:

- **Elevation display zoom** = recursive per-quadrant subdivision: descend while
  `area(child) >= effMaxArea` at *every* level (`:292`), stopping otherwise or at
  `maxZoom = min(source maxZoom, view integer zoom)` (`:290-291`, `stoppedByMaxZoom`). A z16
  leaf requires only that its **z15 parent's own** area cleared the threshold.
- **Vector styling zoom `s`** = a whole-tile aggregate formula applied once the vector source
  hits its own `max_zoom` 14 (`:316-323`):

  ```cpp
  float stepExponent = tileSet.source->overzoomStepExponent();  // default 2.0
  int s = tileId.z + std::max(0, int(std::ceil(std::log2(area/effMaxArea)/stepExponent)));
  visId.s = std::max(std::min(s, _view.getIntegerZoom()), visId.z + zoomBias);
  ```

  i.e. `s = 14 + ceil(log4(A14/M))` — which is the per-quadrant recursion's answer **only if
  screen area splits uniformly 4-ways at every level**.

Under a pitched camera, area does *not* split uniformly: the near part of a tile's footprint
concentrates screen area. Worked example: a z14 tile with total area `A14 = 3M` gets
`s = 14 + ceil(log4(3)) = 15`, so `zt = min(16, 15, 14+kLevels) = 15`. If its near z15 quadrant
holds 40% of that area (`A15 = 1.2M ≥ M`), the elevation traversal subdivides it and (at view
zoom ≥ 16) emits **z16 leaves** there. The drape's ceiling is z15; the terrain under it renders
z16. The `ceil()` step boundaries guarantee there is always a band of tiles in this regime under
tilt; it worsens toward grazing, and it sits exactly where the camera is looking (the
concentrated region) — for the Matterhorn repro framing, the summit.

Two aggravators, both live-persisted config knobs (`~/.config/Ascend/config.yaml`, the same
confound class CLAUDE.md documents for `sources.last_source`):

- `lodAreaBias` (the per-source "resolution retention" GUI sliders;
  `tileManager.cpp:276-287`, `tileSource.h:53,154-157`; `zoom_retention: 1.0` in
  `elevation.yaml:40`). An elevation bias > 1 or an `osm` bias < 1 widens the same gap with no
  code change at all.
- `overzoomStepExponent` is also per-source and live-settable (`tileSource.h:64,159-160`); > 2
  makes `s` lag further.

### 1.4 What the mismatch does to the composite's content

With the summit cell left at `zt = 15`, `buildOverzoomElevationMosaic`'s stitch-input loop
(`rasterSource.cpp:1095-1122`) walks `for (za = cellTargetZoom(row,col); za > _primary.z; za--)`
doing **cache-only** `getTexture()` lookups (deliberately never fetching, `:1093-1094`):

- Best case: the z15 texture is still in the weak cache (`m_textures`,
  `rasterSource.cpp:770-808` — weak refs, erased the instant the last strong ref dies) → the
  drape renders the **z15** PL surface while the terrain renders **z16**: a one-level DEM delta.
- Worse case (and plausible in steady state, since nothing *displays* z15 there anymore and so
  nothing holds a strong ref to it): z15 evicted → the cell falls through to the vector tile's
  own **z14** texture, triangle-PL-upsampled (`:1115-1120`) → a two-level delta.

At z14, DEM lattice nodes under the summit are ~38 m apart (the 64-node-per-tile PL surface of
`fillLatticeUpsampledCell`/`buildLatticeNodes`, `rasterSource.cpp:174-248`); a piecewise-linear
surface through that lattice undershoots the Matterhorn's summit pyramid by a real, fixed
100-400 m. That magnitude sits comfortably inside the bracketing from the nudge experiments
(+50 m: no visible effect; +1000 m: gap fully closed) and explains why the deficit is a smooth
function of terrain sharpness: PL upsampling is *exact* on locally-planar terrain (gentle base →
red/black contours align) and worst at high curvature (summit).

### 1.5 Why every prior negative result is consistent with this mechanism

- **Raising `kOverzoomCompositeMaxLevels` 2→4, elevation `max_zoom` 16→18, `terrain_grid`
  256→1024 changed nothing (confirmed fact 3):** all three raise *other* terms of
  `zt = min(maxZoom, s, z + kLevels)` or the mesh density. `s` — the binding term in the
  mismatch regime — is untouched by all three. A clean no-effect is exactly what this predicts.
- **Attachment zoom always exact (facts 4-5, zero hits on both `style.cpp` z-mismatch logs):**
  this mechanism never touches `raster.tileID`; the attachment is a same-footprint composite
  registered at z14 with `u_raster_offsets.z == 1.0` throughout. Consistent.
- **Mosaic toggle irrelevant (fact 6):** both branches of the rebuild call
  (`tileManager.cpp:1149-1158`) feed the same capped `zt`/caps into the same composite; the
  texture-shading mosaic only wraps it (`buildElevationMosaic` reaches for the composite as its
  center cell, `rasterSource.cpp:845-847`). Consistent.
- **Texture size grows with `s` and plateaus (fact 7):** the observed sizes 768/1536/3072/6144
  are exactly `3 × 2^(zt-14) × 256` for `zt` = 14/15/16/17 — the composite is always
  *allocated* at full `zt` resolution regardless of what data fills it
  (`stitchElevationOverzoomMosaic`, `rasterSource.cpp:439-479`, upsamples missing cells rather
  than shrinking). **The size log proves only `zt`; it says nothing about whether the summit
  cells hold real `zt`-level data or z14 upsample, and nothing about what zoom the terrain
  tileset displays underneath.** This is the exact gap between what round 7 verified and what
  matters.
- **Divergence shape (fact 8):** worst at the summit (the concentrated, finest-displayed,
  sharpest-curvature region), matching at the base (planar terrain → PL upsampling exact;
  and/or the farther region where displayed zoom ≤ `zt` so the caps genuinely work). The
  specific "red contour spacing compressed relative to black going up the mountain" reading is
  *consistent* with a coarse-PL flank profile (a chord across a steepening-then-apex profile
  stays steep where the true surface eases off), but I have not derived that signature
  rigorously — flagged as supporting, not probative.

## 2. Angle 1 as originally posed (caps landing too low): traced, and mostly cleared

- **`rendered`/`ready` desync with what's actually on screen:** `rendered` is deliberately the
  traversal target (not the per-frame proxy set) with per-tile readiness checked against
  `elevSet->tiles` (`tileManager.cpp:1050-1092`); the known `s`-normalization pitfall is already
  fixed (`:1073-1085`). Remaining desyncs (e.g. the renderer proxying with a **child** —
  `updateProxyTiles` does add child proxies, `tileManager.cpp:1211-1213` — while the cap
  readiness walk only searches **ancestors**, `:1125-1131`) are real but transient: they change
  only on load/evict events and cannot survive the "stable at rest" observation.
- **Ancestor-walk choosing a different ancestor than the renderer's own proxy fallback:**
  possible in principle, but again only during load gaps — transient, same reasoning.
- **One genuine silent hole, checked and judged unlikely here:** if the elevation source were
  found only in `m_auxTileSets` (`:1034-1037`), its `visibleTiles` is never populated (the
  traversal loop `:272` iterates `m_tileSets` only) → `rendered` empty → caps **silently
  disabled** (`:1098` guard; the `LOGW` at `:1046` fires only when the source is in *neither*
  list). In `stylus-osm-terrain` the elevation source is displayed by terrain-ground/hillshade,
  so it is a real tileset; and cap-less means "finest cached", which errs *fine*, not coarse —
  the wrong sign for this bug. Noted for completeness.

## 3. Angle 2 (screen-area LOD vs steep terrain): the useful half survives

The "projected area of a near-vertical face" concern as posed affects the elevation and vector
sources *identically* (both use `getTileScreenArea`, which projects a flat quad at the
center-screen elevation estimate, `view.cpp:632-636` — no per-tile terrain steepness enters at
all), so it cannot by itself split the two surfaces. What *does* split them is §1.3: the same
area numbers consumed by two different algorithms (recursive per-quadrant vs whole-tile
aggregate). Note also `view.cpp:665`: at `m_pitch == 0` the function returns `FLT_MAX` for every
on-screen tile — the entire area machinery is bypassed at exact nadir, which is what makes §6's
prediction sharp.

## 4. Angle 3 (indexing/orientation): traced, no bug found

The cap grid is built row-major with `cx = baseX + col`, `cy = baseY + row`
(`tileManager.cpp:1100-1104`; slippy `y` grows southward, matching `OverzoomCell`'s
"row: north → south" convention) and consumed with identical indexing
(`rasterSource.cpp:1055-1059`, `:1096-1104`). The north/south flip is internal to
`copyOverzoomGridCell`/`fillLatticeUpsampledCell` (`rowOff = (N-1-row)*Wp`,
`rasterSource.cpp:230`, `:270`) and applied uniformly to both the verbatim-copy and upsample
paths. The `>> dl` ancestor arithmetic is the standard floor-shift and is used consistently on
both sides. Also, as anticipated in the task brief, an indexing bug would produce
patchy/kaleidoscopic misassignment, not the observed smooth monotonic-with-steepness pattern.

## 5. What this round did NOT verify (honesty section)

- I did not (cannot, per policy) run the app. Everything above is static code trace; the
  mechanism in §1.3 is *derived*, with a worked numeric example, not observed live.
- Whether the trusted contour dataset (fact 8) was captured at exactly `--view.tilt 0`. At exact
  pitch 0 the derived mechanism is inert (§3), so if that dataset is nadir-clean, this
  hypothesis cannot be its explanation and §8's fallbacks lead. The camera-state persistence
  confound documented in CLAUDE.md cuts both ways here: a session believed to be at tilt 0 may
  not have been, and vice versa.
- Whether the summit cells' composite content is in fact fallback data (the direct consequence
  claimed in §1.4). Fact 7's size log cannot see this; §7.2's diagnostic can.

## 6. Falsifiable predictions of the leading hypothesis

1. At exact `--view.tilt 0`, after tiles settle, the red/black contour divergence at the summit
   should collapse to the small curvature-proportional PL residual (§8.2) — because `s`,
   displayed elevation zoom, and `zt` all equal `min(16, view integer zoom)` when every
   on-screen area is `FLT_MAX`.
2. Under increasing tilt at view zoom ≥ 16, the divergence should appear specifically on tiles
   whose `s` lags the displayed elevation zoom under their near/steep portion, and §7.1's log
   line should fire for exactly those tiles while it is visible.
3. The divergence should heal, per-tile and stepwise, as continued zoom-in pushes that tile's
   `s` up to 16 (both ceilings saturate) — consistent with the original "waterline recedes as
   you zoom in" report if the waterline's edge tracks the `s`-lagging band.

## 7. Proposed diagnostics (NOT added — for Sebastian or a follow-up agent to add in one pass)

### 7.0 Zero-code, first

Re-run the current contour repro twice with camera state forced from the command line: once with
`--view.tilt 0`, once tilted (e.g. 45), same location (Matterhorn,
`--view.lat 45.9763 --view.lng 7.6586`, zoom 16). Predictions 1/2 above distinguish the
hypothesis immediately. Also worth confirming what `~/.config/Ascend/config.yaml` currently
holds for the per-source retention sliders (`lodAreaBias`) — non-1.0 values widen or narrow the
same gap and would modulate the repro's strength between sessions.

### 7.1 The decisive log line (leading hypothesis)

In `TileManager::upgradeAttachedRasters`, immediately after the `rendered`/`ready` references
are taken (`tileManager.cpp:1097`, before the `if (!rendered.empty())` at `:1098`):

```cpp
// TEMP DIAGNOSTIC (Matterhorn drape investigation, round 8): fires whenever the elevation
//  tileset is DISPLAYING tiles finer than this drape composite's own target zoom zt under
//  this tile's footprint -- the one regime the cap machinery cannot represent (its walk
//  starts at zt and only goes coarser; finer-displayed cells fall into the zTarget < 0
//  "leave at zt" branch below with no trace).
int finestDisplayed = -1;
for (const TileID& r : rendered) {
    if (r.z <= zt) { continue; }
    int ddl = r.z - int(livePrimary.z);
    if ((r.x >> ddl) == livePrimary.x && (r.y >> ddl) == livePrimary.y) {
        finestDisplayed = std::max(finestDisplayed, int(r.z));
    }
}
if (finestDisplayed > zt) {
    LOGD("Drape ceiling below displayed terrain: tile %s zt=%d displayed=z%d (liveS=%d effectiveS=%d)",
         _liveId.toString().c_str(), zt, finestDisplayed, int(_liveId.s), int(effectiveS));
}
```

(`effectiveS` is in scope from `:939`; `rendered` holds normalized 3-arg ids so `r.z` is the
real data zoom.) Cost: a few dozen set-iterations per visible draped tile per update — nothing.
**If this fires persistently for the summit tile (8540/5830/14 or its neighbors) while the
divergence is visible, the hypothesis is confirmed; if it never fires during a confirmed repro,
it is refuted** and §8 takes over.

### 7.2 The per-cell content log (fallback, and independently valuable)

In `RasterSource::buildOverzoomElevationMosaic`'s stitch-input loop
(`rasterSource.cpp:1095-1122`), tally what each cell actually got, and append the tally to the
existing composite LOGD at `:1140`:

- declare `int cellsExactAtZt = 0, cellsCapped = 0, cellsUpsampled = 0, cellsOwnFallback = 0;`
  before the loop;
- in the loop: when a texture is found with `za == zt` → `++cellsExactAtZt`; found with
  `za == cellTargetZoom(row, col)` but `< zt` → `++cellsCapped` (real data at the capped
  displayed zoom); found with `za < cellTargetZoom(row, col)` → `++cellsUpsampled` (cache miss
  forced a coarser ancestor); `!found` → `++cellsOwnFallback`;
- extend the LOGD format with `" cells exact=%d capped=%d upsampled=%d own=%d"`.

Filtering the log for the summit tile then directly answers the question fact 7's size log could
not: does the summit region's composite hold real target-zoom data, real-but-capped coarser
data, ancestor upsample, or own-z14 fallback? This discriminates *every* remaining candidate
(ceiling-too-low vs cache-miss vs content-actually-fine) in one run, and is the right follow-up
regardless of §7.1's outcome.

## 8. Fallback candidates if §7.1 refutes the leading hypothesis

1. **Weak-cache content starvation:** caps correct (= displayed zoom), but the stitch's
   cache-only `getTexture()` misses the displayed tiles' textures (weak refs with an
   erase-on-death deleter, `rasterSource.cpp:782-789`) at the moments composites rebuild,
   leaving upsampled content that the coverage signature (`:1068-1077`) would only repair when
   the texture reappears in cache — persistent if eviction cycles. §7.2 distinguishes this
   directly (cells reported `upsampled`/`own` despite caps at displayed zoom).
2. **The contour methodology's own baseline:** red (`getTerrainMeshElevation`, the
   64-per-tile-equivalent lattice-PL surface — `stylus-osm.yaml:810`) vs black
   (`getElevation()`, a full-resolution per-fragment point sample — `hillshade.yaml:384`) differ
   *by construction* by the lattice's curvature-proportional PL undershoot even when the drape
   is perfect, since black plots the raw field, not the terrain mesh's rendered surface. At z16
   display (~9.5 m lattice pitch) this residual is small (tens of meters at the apex — the
   already-accepted "~1% at cols/summits" residual), so it cannot be the whole story for a
   50-1000 m deficit — but it contaminates the contour data's fine structure and should be kept
   out of any quantitative read of it. A variant black reference that plots
   `getTerrainMeshElevation` in `terrain-ground`'s own shader would compare surface-to-surface
   instead. (Fact 1's depth-test evidence is immune to this caveat: terrain-ground *winning* the
   depth test broadly proves the two rendered surfaces genuinely differ.)
3. **Readiness/child-proxy transients** (§2) — only if the "stable at rest" premise turns out
   weaker than believed.

## 9. Candidate fix directions (NOT to be implemented without sign-off)

### (a) Fold the displayed elevation zoom under the footprint into the composite target

In `upgradeAttachedRasters`, compute the finest displayed elevation zoom over the footprint
(§7.1's loop — it needs only the `rendered` set that is already built) and raise the effective
target, e.g. `livePrimary.s = max(livePrimary.s, finestDisplayedUnderFootprint)` before the `zt`
computation at `:1017`. The existing cap machinery then correctly clamps the *coarser-displayed*
cells back down, so only the genuinely-finer-displayed cells gain resolution.

- **Costs/risks:** `zt` rises for tilted views → larger composites and re-stitches when the
  displayed set changes (bounded by the same "visibleTiles changes only when the view changes"
  stability argument the caps already rely on, `:1053-1058`). Crucially, the polygon mesh's own
  tessellation `gridRes` (`polygonStyle.cpp`, derived from `id.s` at tile-build time) would
  still lag the finer lattice — the scene comment (`stylus-osm.yaml:646-659`) is explicit that a
  polygon triangle spanning more than one lattice cell chords across surface folds. So (a) needs
  a coordinated story for `gridRes` (rebuild on the bumped `s`, or accept bounded chording),
  which is real design work in code with a regression history. Also re-binds
  `kOverzoomCompositeMaxLevels` (z14+2=16) as the ceiling — fine while elevation data tops out
  at z16, but worth stating.

### (b) Fix the divergence at its source: make the vector `s` formula agree with the recursive traversal

E.g. compute the overzoom step from the *maximum child-quadrant* area (or recurse the same way
the subdivision itself does) instead of the whole-tile aggregate, in `getVisibleTiles`
(`tileManager.cpp:316-323`).

- **Costs/risks:** raises `s` for many tilted-view tiles globally → more full tile *rebuilds*
  (an `s` change is a new cache key and a complete `TileBuilder` pass, per
  `docs/terrain-drape-resolution-plan.md` §1) and more GPU load in exactly the `unlit-polygons`
  hot spot the profiling work is trying to shrink. Blunter and more expensive than (a); touches
  every source's LOD behavior, not just the drape.

### (c) Symptom mask: depth-bias the drape over terrain-ground

Already sketched as option (c) of `docs/terrain-drape-resolution-plan.md`. Hides the lost depth
test but leaves the drape geometrically wrong (the red/black contours would still diverge), so
`rock-texture`'s slope/curvature reads stay wrong exactly where it matters most. Cheap, low
risk, unsatisfying.

## 10. Recommendation (for Sebastian's sign-off, not a decision)

Run §7.0 (zero-code, two command lines) and, in the same session, add §7.1's single log line —
together they confirm or refute the leading hypothesis decisively and cheaply. Add §7.2's tally
at the same time if convenient: it discriminates all remaining candidates regardless of outcome
and costs a couple of lines on an existing LOGD. If the hypothesis confirms, my inclination is
(a): it is contained inside the mechanism that already exists for exactly this class of
agreement problem, reuses the already-built `rendered` set, errs only in the direction of *more*
fidelity, and leaves the global LOD heuristics untouched — with the explicit caveat that the
`gridRes` coordination question must be answered as part of its design, not discovered after.
But that is contingent on §7's data; per this investigation's track record (seven rounds,
several confident-but-wrong conclusions), no fix should be started on the strength of this trace
alone.

Suggested repro command lines (per CLAUDE.md conventions; tilt deliberately varied — that
variation is the point of §7.0, overriding the default tilt-0 rule for this specific test):

```
./build/Debug/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 16 --view.rotation 0 --view.tilt 0  --sources.last_source stylus-osm-terrain
./build/Debug/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 16 --view.rotation 0 --view.tilt 45 --sources.last_source stylus-osm-terrain
```
