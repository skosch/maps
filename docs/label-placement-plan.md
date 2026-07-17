# Mountain Peak & Water Label Placement — Implementation Plan

## Background

Peak, lake, and area labels currently use a fairly generic label pipeline inherited from
tangram-es: peak priority is weighted only by raw elevation (`stylus-osm.yaml:1456-1488`,
`priority: function() { return global.priority.peak + (1 - feature.ele/16384.0); }`),
anchor fallback on collision is a fixed list that reacts only to other labels — never to
what's visually underneath (ridgelines, water, trails), peak names are hard-truncated
with `…` past 18 characters, halos are a flat per-label stroke color with no awareness of
what's behind them, and lake/park names are single centroid points rather than classic
curved/tracked cartographic labels.

Goal: bring peak/water/area labeling toward professional topographic-map practice,
reusing the live texture-shading work (`docs/texture-shading-plan.md`, now merged to
`master`) as a ranking and placement signal wherever the user's stated intent calls for
"using the texture shading values as a source" — not a separately-invented heuristic.

**Non-goals** (explicitly out of scope, do not attempt):
- Mountain ridge line labeling (`natural=ridge`/`arete`). OSM tagging coverage is too
  sparse to be worth an ingestion pass, and raster ridge-line extraction (skeletonization)
  is its own multi-week research project. Revisit later as a separate effort.
- True saddle-based topographic prominence (the rigorous definition, requiring a global
  flood-fill/watershed search for the key saddle separating a peak from any higher peak).
  We use the real OSM `prominence` tag when present, and the CPU texture-shading value as
  a *local* proxy otherwise — good enough for label *ranking*, not a geodesy-grade
  prominence database.
- Do not merge to `master` or push to any remote without explicit human approval (same
  standing rule as all other in-flight work).

This project builds on `docs/texture-shading-plan.md`, which is now merged into
`master` (2026-07-12) — the elevation-mosaic-stitching infrastructure (`RasterSource`,
`m_buildElevationMosaic`, `m_keepTextureData`) it introduced is a **hard prerequisite**
for Phases 2 and 3 below. Read that doc first if you haven't; this plan assumes its
Frozen Interface Contract holds.

## Frozen Interface Contract

**All phases must conform to this. If a phase discovers it needs to change, it MUST
update this section and flag the change prominently in its final report.**

### Shared primitive: CPU texture-shading sampler

**Updated by Phase 2 (implemented) — read this before starting Phase 3, the signature and
threading model below are now final, not aspirational.**

Phases 2 and 3 both need to evaluate "how strongly does texture shading emphasize this
point" from CPU code. This must be a literal CPU port of the already-frozen shader formula
in `docs/texture-shading-plan.md` — not a new heuristic (e.g. not a simple local min/max
relief calculation) — so that label-placement decisions stay visually consistent with what
the hillshade layer actually renders.

- New file: `tangram-es/core/src/util/textureShading.h/.cpp`, as originally proposed (not
  folded into `elevationManager.h/.cpp` — kept separate so it can be unit-tested directly
  against hand-built `Texture` mosaics, the same way `stitchElevationMosaic()` is tested in
  `tests/unit/rasterMosaicTests.cpp`, without pulling in `ElevationManager`'s GL/render-state
  machinery).
- **Actual signatures** (two functions, not one — see "Threading" below for why):
  ```cpp
  // Pure, RasterSource-independent core: evaluate the formula directly against an
  // already-stitched 3Wp x 3Wp mosaic buffer at worldPos (must fall within tileId's own
  // footprint). Unit-tested directly with hand-built Texture objects.
  float sampleTextureShadingAtMosaic(const Texture& mosaic, TileID tileId, ProjectedMeters worldPos);

  // Looks up the mosaic for worldPos via RasterSource::getRaster()/getExistingMosaic()
  // (cheap, reuse-only — never triggers a new stitch), then delegates to the above.
  // ok=false if no mosaic is currently resident at that position.
  float sampleTextureShading(RasterSource& elevationSource, ProjectedMeters worldPos, bool& ok);
  ```
  Note `RasterSource&`, not `const RasterSource&` as originally sketched:
  `getExistingMosaic()`/`getRaster()` aren't `const`-qualified on `RasterSource` (they do
  `map::find` + `weak_ptr::lock`, not logically-const enough to bother changing).
- **Formula**: band weight `pow(2.0, -k * alpha)`, contrast curve
  `az = contrast * z; shade = az / (2*sqrt(1+az*az)) + 0.5` — reused as literally as
  possible, but see two **deliberate, documented deviations** from a naive port, both
  explained in `textureShading.cpp`'s comments:
  - **`alpha`/`contrast` are hardcoded to 0.6/1.0, not read from the live scene config.**
    These are not the values still written in *this* contract as of the original Phase 2
    prompt (0.75/0.05) — those were already stale: `docs/texture-shading-plan.md`'s own
    "Resolution of the open follow-ups" section records the shipped default as having moved
    to `u_texture_shading_alpha: 0.6` / `u_texture_shading_contrast: 1.0` on 2026-07-06,
    *without* the top-level Frozen Interface Contract section of that file being updated to
    match — a real doc/code drift, not a Phase 2 invention. This sampler uses the values
    that are actually shipped in `hillshade.yaml` today, since matching what the hillshade
    layer actually renders (this feature's whole stated purpose) requires the current
    values, not the stale documented ones. **`docs/texture-shading-plan.md`'s Frozen
    Interface Contract section should be corrected to match its own "Resolution" section
    the next time someone touches that file** — not done here, out of scope for this
    plan/branch, flagged instead.
  - **No zoom-continuity, no auto-contrast.** The live shader (`hillshade.yaml`'s
    `textureShading()`) has grown well beyond this contract's original formula since it was
    written: a fractional base LOD anchored to continuous view zoom, zoom-dependent
    `alpha`/`alpha_min` blending, and a live `u_texture_shading_auto` contrast multiplier
    driven by `RasterSource::aggregateRugosity()`. This sampler intentionally does **not**
    replicate any of that: it always evaluates at the mosaic's native (level 0) resolution
    with fixed alpha/contrast, because a peak-priority ranking needs one stable number per
    peak, not a value that changes as the camera pans/zooms/tilts. This is a scope
    simplification, not a bug — flagged clearly in `textureShading.h`'s doc comment for
    Phase 3 to be aware of if it reuses this sampler for anchor-cost scoring.
- Octave levels are produced by **software box-filter downsampling** of the raw CPU pixel
  buffer (`Texture::bufferData()`), 2×2 averaging repeated `kMaxLevels = 4` times (the
  contract's original, conservative `TEXTURE_SHADING_MAX_LEVELS` default — *not* the
  shader's current, larger `TEXTURE_SHADING_MAX_LEVELS = 8`, which is only safe there
  because of the shader's fractional base LOD; see `textureShading.cpp` for the full
  reasoning).
- Operates on the same `3Wp×3Wp` mosaic buffer layout the texture-shading plan established
  (tile's own data in the center third at `muv = (uv+1)/3`, real-or-mirror-extrapolated
  neighbors around it) — no new buffer layout introduced.
- **Threading — resolved, this was the single biggest open risk in this plan.**
  `TextStyleBuilder`/`PointStyleBuilder::addFeature` run on background `TileWorker` threads
  (confirmed: `tileWorker.cpp`'s `TileWorker::run()` calls `task->process(*builder)`, which
  is what invokes `TileBuilder::build()` → `applyStyling()` → `StyleBuilder::addFeature()`,
  entirely inside the worker loop). Meanwhile `RasterSource::m_textures`/`m_mosaics` — and
  therefore `getTexture()`/`getExistingMosaic()`, which the sampler needs — are documented
  and relied upon elsewhere in this codebase as **main-thread-only**: `rasterSource.h`'s own
  comments state mosaics are built/patched "Main-thread only, like all other m_mosaics
  access", and `RasterTileTask::addRaster()` (which does the actual stitching/patching) only
  ever runs from `complete()`/`complete(TileTask&)`, invoked from
  `TileManager::TileEntry::completeTileTask()` on the main thread — never from
  `TileWorker::run()`. Separately, even ignoring thread-safety, the elevation raster
  subtask is attached to a vector tile's `Tile::rasters()` inside that same main-thread
  `complete()` call, which runs *after* `process()` (where `addFeature()` runs) has already
  finished for that tile — so at the point `PointStyleBuilder::addFeature()` executes, the
  peak's own tile doesn't even have a raster/mosaic attached yet, safety aside.
  **Conclusion: calling the sampler from `TextStyleBuilder`/`PointStyleBuilder::addFeature`
  is unsafe and was not attempted.** The documented fallback was used: priority refinement
  happens once per visible peak **on the main thread**, in
  `LabelManager::processLabelUpdate()` (piggybacking on the same per-label loop that already
  does `Label::setElevation()`/`m_elevationSet` for terrain height, right next to it),
  cached on the label via a new `Label::m_prominenceRefined` flag mirroring
  `m_elevationSet`'s "retry every frame until success, then stop" pattern. See Phase 2's
  writeup below for the full mechanism (new `Label::Options::refinePriorityWithTextureShading`
  flag, new `priority_texture_shading` style param, `Label::refinePriority()`).
- **Availability fallback**: if the elevation source/mosaic isn't loaded for a given peak's
  location (flat/non-terrain map style, or tile not yet cached), `ok = false` and callers
  fall back to today's behavior (Phase 2: pure elevation weighting; Phase 3: skip the
  texture-shading term, use only the vector-feature-proximity term). Implemented in Phase 2
  as: the tile-build-time `priority:` JS function always computes elevation-only priority as
  a *provisional* value first; `Label::refinePriority()` overwrites it later, only once
  `sampleTextureShading()` returns `ok = true` for that peak's position — permanently
  provisional (never refined) is a legitimate, expected steady state for non-terrain map
  styles, not an error.

### Shared primitive: anchor candidate geometry

Phase 3 needs a canonical list of candidate anchor screen-space footprints to score. Use
the same 8-direction set already implied by `Anchor` (`labelProperty.h`):
`{top, bottom, left, right, top_left, top_right, bottom_left, bottom_right}` (skip
`center`, which is the icon's own position). Compute each candidate's footprint as the
label's measured text bounding box offset from the icon anchor by
`icon_radius + half_label_extent` in the anchor's direction — reuse
`LabelProperty::anchorDirection()` (`labelProperty.cpp:35-51`) for the direction vector
rather than re-deriving it.

### Addendum from Phase 3 (implemented — read before Phase 6 integration)

Phase 3 (salience-aware anchor ordering) is implemented on branch
`label-placement-phase3-anchors`. Two clarifications to the contract above, plus one
noted deviation:

- **Architecture question resolved**: a single style's `StyleBuilder::addFeature` pass
  (e.g. `peak`'s `PointStyleBuilder`/linked `TextStyleBuilder`) does **not** have access
  to other layers' raw feature geometry — it only ever sees the feature(s) its own
  matched draw rule(s) passed it. The answer is the shared per-tile pre-pass the plan
  anticipated: `TileBuilder::build()` (`tangram-es/core/src/tile/tileBuilder.cpp`) already
  receives the tile's full `TileData` (all layers together, since this app's whole vector
  schema is one `osm` source) before any style's `setup()`/`addFeature()` runs. A new
  `AnchorOccupancyGrid` (`tangram-es/core/src/labels/anchorOccupancyGrid.h/.cpp`) is built
  once there from the `water`/`transportation` layers (coarse 16x16 grid over the tile's
  normalized `[0,1]` local space, line/polygon-boundary rasterization only, no fill) and
  attached to the `Tile` object itself (`Tile::setAnchorOccupancyGrid`/
  `anchorOccupancyGrid()`), which every style's builder already receives in `setup(const
  Tile&)` — so `TextStyleBuilder` just reads it off the tile it's already handed.
- **Anchor-cost hook point**: not `applyRule()` (`textStyleBuilder.cpp` ~684-733) as
  originally scoped — that function runs before any feature geometry/position is known,
  so it cannot see where a label will actually land. The actual reordering happens at the
  call sites that *do* have a position, right before each `addLabel(...)` call:
  `PointStyleBuilder::addFeature` (icon+text, e.g. peak labels) and
  `TextStyleBuilder::addFeature`'s point/polygon-centroid branches (standalone text).
  Both call the new public `TextStyleBuilder::salienceOrderedAnchors(...)`, which returns
  `Options::anchors` re-sorted by ascending cost; the existing anchor-cycling mechanism
  (`Label::nextAnchor()`, `LabelManager::handleOcclusions`) is untouched.
- **Deviation from the Frozen Interface Contract's `sampleTextureShading` signature**:
  Phase 3's stub (`tangram-es/core/src/util/textureShading.h/.cpp`) takes
  `const RasterSource*` (nullable pointer) instead of `const RasterSource&`. Phase 3 has
  no elevation `RasterSource` plumbed into `TextStyleBuilder`/`PointStyleBuilder` (that's
  Phase 2/6's job, including the threading-safety question), so a nullable pointer is
  what's actually available; the contract's own text permits adjusting types "to preserve
  this shape." Phase 3's only caller always passes `nullptr`, so the term always
  contributes 0 today. **Phase 6 must decide** whether to keep the pointer or restore the
  reference once a real source exists, and update this note accordingly.

## Phase 1 — Fonts & unbounded wrapping

**Objective:** switch peak labels to IBM Plex Sans Condensed SemiBold and water labels to
IBM Plex Serif Italic, and make long/multi-language names wrap instead of truncate.

**Files:**
- `assets/shared/fonts/` — copy in:
  - `~/.fonts/IBM-Plex-Sans/IBMPlexSans_Condensed-SemiBold.ttf`
  - `~/.fonts/IBM-Plex-Serif/IBMPlexSerif-Italic.otf`
- `assets/scenes/stylus-osm.yaml`:
  - `fonts:` block (~line 338-346): add two new family entries, e.g.
    ```yaml
    IBM Plex Sans Condensed:
        - { weight: 600, url: shared/fonts/IBMPlexSans_Condensed-SemiBold.ttf }
    IBM Plex Serif:
        - { weight: 400, style: italic, url: shared/fonts/IBMPlexSerif-Italic.otf }
    ```
  - `peak` draw group (~line 1456-1488):
    - `font: { family: "IBM Plex Sans Condensed", weight: 600, ... }`, drop `style:
      italic` (condensed semibold replaces italic as the peak "look"), reduce `size`
      from `10.67px` — condensed + a smaller size is the "smaller and thinner" ask;
      pick something like `9px`/`9.5px` and sanity-check readability in a screenshot
      rather than guessing blind.
    - Remove the JS substring truncation in `text_source`
      (`if(name.length > 18) { name = name.substring(0, 17) + "…"; }`).
    - Remove (or raise far beyond any realistic case) `max_lines: 2` — confirmed at
      `tangram-es/core/src/style/textStyle.h:47` that `maxLines` defaults to `0`
      (unlimited) and the ellipsis-truncation path in
      `tangram-es/core/src/text/fontContext.cpp:182-204` **only fires when explicitly
      set** — so this alone removes engine-level truncation, no C++ change needed.
      Elevation should probably stay on its own trailing line (keep the existing
      `\n`-joining logic in `text_source`), just don't cap total lines.
  - `water_name` / `waterway_name` / their `oceans`/`intermittent` sub-rules (~line
    626-697) and the lake-adjacent `stylus-osm.yaml:1540-1541` block: switch
    `font.family` to `"IBM Plex Serif"`, keep `style: italic`. Check
    `global.color.water_name` (currently `#51909c`, `stylus-osm.yaml:42`) — this reads as
    a muted teal, not "dark blue as is tradition." Propose 2-3 concrete dark-blue swatches
    (e.g. something in the `#1a3c5e`–`#2b4c7e` range) and show a screenshot comparison
    rather than picking one blind.

**Acceptance criteria:**
- Scene loads without font/YAML errors (check app log).
- Headless screenshot (existing testenv pattern) over an area with at least one very
  long/multi-word peak name and one non-Latin name: confirm it wraps across multiple
  lines rather than being cut off with `…`.
- Screenshot comparison of peak label font before/after (condensed semibold, smaller),
  and water label font before/after (serif italic, new color swatch options).
- `make -f tests.mk` still passes.

## Phase 2 — Prominence-weighted peak priority

**Implemented (branch `label-placement-phase2-prominence`, off `master`). Summary of what
was actually built, and two corrections to the design prose below:**

- **`feature.prominence` availability was mis-cited, but the underlying claim holds.** The
  design section below says it's "already parsed e.g. `osm-place-info.js:159-160`" — that's
  wrong: that file's `prominence` read is inside `osmPlaceInfoCb()`, a runtime callback for
  the tap-to-query info-panel plugin that parses a *live Overpass API HTTP response*, not a
  vector-tile feature property. However, `assets/scenes/stylus-osm.yaml`'s own `peak` draw
  rule already filters on `{ prominence: true }` (`filter.all[0].any`, gating which peaks
  show below z12) — proof the vector tile schema already carries `prominence` as an ordinary
  feature property, in the exact same `feature.*` namespace as `feature.ele`, reachable from
  the `priority:` JS function with **no C++ parsing change needed**. So the "??" fallback
  chain's first branch was free; no `mvt.cpp`/`geoJson.cpp`/`StyleContext` attachment
  mechanism needed to be added for it.
- **The synthetic-property idea in the design's "Files" note below was not used.** Rather
  than attaching a `feature.__texture_shading` property before the JS `priority:` function
  runs (which would require computing it at tile-build time — impossible per the Frozen
  Interface Contract's threading resolution above), priority is instead a **two-stage**
  value: the JS `priority:` function computes a final value immediately if
  `feature.prominence` is present (no threading concern, see above), or a *provisional*
  elevation-only value otherwise; `Label::refinePriority()` then overwrites the provisional
  value later, on the main thread, once the CPU sampler succeeds for that peak's position.
  The mechanism that tells C++ "this label wants that later refinement" **is** a new
  synthetic bit, just not a `feature.*` JS property — see below.

**Files actually touched:**
- `tangram-es/core/src/util/textureShading.h/.cpp` (new) — the CPU sampler, per the Frozen
  Interface Contract above.
- `tangram-es/core/src/labels/label.h/.cpp` — new `Label::Options::refinePriorityWithTextureShading`
  flag; new `Label::m_prominenceRefined` flag + `Label::refinePriority()` method; new
  `Label::m_trackRelativePriority` flag + `Label::syncRelativePriority()` method (needed
  because `setRelative()` only copies an icon's priority into its linked text label
  **once**, at tile-build time — a peak's text label needs to keep re-copying it every frame
  so it doesn't go stale once `refinePriority()` starts mutating the icon's priority later,
  on the main thread, in a different `LabelManager::processLabelUpdate()` call for a
  different style).
- `tangram-es/core/src/labels/labelManager.cpp` — `processLabelUpdate()` calls
  `label->syncRelativePriority()` for every label, and `label->refinePriority(...)` next to
  the existing `label->setElevation(...)` call, same "retry every frame until success, then
  cache" pattern as `m_elevationSet`.
- `tangram-es/core/src/scene/styleParam.h/.cpp` — new `StyleParamKey::priority_texture_shading`
  boolean style param (mirrors `collide`/`flat`'s existing pattern exactly).
- `tangram-es/core/src/style/pointStyleBuilder.cpp` — reads the new style param into
  `Parameters.labelOptions.refinePriorityWithTextureShading`; `addPoint`/`addLine`/`addPolygon`
  suppress it per-feature when `_props.contains("prominence")` (real tag already produced a
  final priority in JS, so the later main-thread refinement must not overwrite it with the
  weaker proxy).
- `assets/scenes/stylus-osm.yaml` — peak draw rule: `priority:` function now branches on
  `feature.prominence`; new `priority_texture_shading: true` opts the peak draw rule into
  the main-thread refinement.

**Original design section (kept for context; superseded by the above where they differ):**

**Files:** wherever the Frozen Interface Contract's shared sampler lands (new
`textureShading.h/.cpp` or `elevationManager.*`), `assets/scenes/stylus-osm.yaml` (peak
`priority:` function), possibly `tangram-es/core/src/style/pointStyleBuilder.cpp` if the
priority computation needs to move from a pure-YAML-JS-function into C++ to get sampler
access (JS `function()` style-param context does **not** have raster access — confirm
this, and if priority must stay computable in YAML/JS, the sampler's result will need to
be attached as a synthetic per-feature property, e.g. `feature.__texture_shading`,
computed in C++ before the JS priority function runs — check how `feature.ele` itself
gets attached, at `mvt.cpp`/`geoJson.cpp` parsing or via `StyleContext`, and follow the
same mechanism).

**Design:**
1. Implement the shared CPU sampler per the Frozen Interface Contract above. This is the
   first and most important deliverable — resolve the worker-thread-safety question
   before building anything on top of it.
2. For each peak feature, compute:
   ```
   prominence_score =
       feature.prominence (real OSM tag, already parsed e.g. osm-place-info.js:159-160)
       ?? sampleTextureShading(...) at the peak's own position
          (a summit is locally convex in every direction, so a strong texture-shading
           response there is a principled proxy for "how much it stands out" — not
           a new heuristic, the literal output of the already-tuned formula)
       ?? fall back to today's elevation-only weighting if neither is available
   ```
3. Fold into the existing tiered-priority idiom exactly like `place_fn`
   (`stylus-osm.yaml:269-273`, sqrt-compression into the fractional part of a
   tier-based priority): `global.priority.peak - compress(prominence_score)`.

**Acceptance criteria:**
- Unit test for the CPU sampler against a synthetic mosaic buffer with a known ridge
  pattern (mirror the style of `rasterMosaicTests.cpp`), asserting it produces values in
  the same ballpark as hand-computing the formula.
- `make -f tests.mk` passes.
- Headless screenshot over a real mountainous area, before/after: confirm visually that
  a few genuinely prominent peaks (e.g. a range's highest point standing well above its
  neighbors) now out-rank nearby, merely-higher-but-unremarkable bumps that previously
  won purely on raw elevation. Call out 2-3 specific before/after examples in the final
  report so Sebastian doesn't have to hunt for them.

## Phase 3 — Salience-aware anchor ordering

**Objective:** order each peak label's anchor fallback list so the least visually
disruptive position is tried first, instead of a fixed compass order.

**Files:** `tangram-es/core/src/style/textStyleBuilder.cpp` (peak/POI label building,
`~line 684-733` for where `Options::anchors` currently gets populated),
`tangram-es/core/src/labels/labelProperty.h/.cpp`.

**Design:**
1. Confirm the exact hook point: `PointStyleBuilder::addFeature`
   (`pointStyleBuilder.cpp:581-624`) builds the icon, `TextStyleBuilder` builds the
   linked child text — the anchor list lives on the text label's `Options`. Investigate
   whether a single style's builder pass has access to the tile's *other* layers' raw
   feature geometry (water/road/trail) for the proximity term, or whether this needs a
   small shared per-tile pass computed once before per-style building begins (e.g. a
   lightweight `TileBuilder`-level helper populating a coarse per-tile occupancy grid
   from all layers' raw features). Document whichever is true — this is the one real
   open architecture question in this phase.
2. For each of the 8 candidate anchor directions (Frozen Interface Contract), compute:
   ```
   cost = w1 * texture_shading_at(candidate_footprint)   // avoid stamping over a ridge
        + w2 * proximity_penalty(candidate_footprint, nearby water/road/trail geometry)
   ```
   texture-shading term uses Phase 2's sampler (evaluated over the small candidate
   footprint, e.g. averaged over a few sample points — not just the center) with `ok =
   false` treated as zero contribution (falls back to only the vector-proximity term,
   independent of Phase 2 being available). The vector-proximity term has no dependency
   on Phase 2 or the mosaic and could land even if Phase 2 is deferred.
3. Sort `Options::anchors` by ascending cost and hand it to the existing mechanism
   unchanged — `Label::nextAnchor()` (`label.cpp:110-115`) and the collision-retry loop
   in `LabelManager::handleOcclusions` (`labelManager.cpp:477-521`) already do the rest.
   **No changes to the runtime collision/occlusion code should be needed** — if you find
   yourself modifying `handleOcclusions`, stop and reconsider; that's a sign the anchor
   list isn't being consumed the way it's designed to be.

**Acceptance criteria:**
- `make -f tests.mk` passes; no changes needed/expected in label-collision unit tests.
- Headless screenshot over a mountainous area with peaks near rivers/trails/steep
  ridgelines: confirm labels visibly prefer sitting away from those features compared to
  the current fixed `{bottom, top, right, left}` order. Call out specific examples.

## Phase 4 — Background-adaptive per-pixel halo

**Objective:** halo/outline visibility should fade in over dark backgrounds and out over
light ones, decided per-fragment against the actual composited pixel underneath — not a
per-label flag.

**Files:** `tangram-es/core/src/map.cpp` (`Map::render`, ~line 283-352),
`tangram-es/core/src/scene/scene.cpp` (`Scene::render`, style draw loop ~line 694-710),
`tangram-es/core/src/style/textStyle.h/.cpp` (`TextStyle::onBeginDrawFrame`,
`UniformBlock`), `tangram-es/core/shaders/sdf.fs`, `tangram-es/core/shaders/text.vs`,
`tangram-es/core/src/gl/framebuffer.h/.cpp` (reuse pattern, don't reinvent — this exact
render-to-texture-then-sample approach already exists for the selection buffer and
`ElevationManager`'s terrain depth pass).

**Design:**
1. Add a new offscreen color `FrameBuffer` sized to the viewport (recreate on resize,
   mirroring how the selection buffer/depth buffer already handle resize).
2. After all non-text styles have drawn into the default framebuffer for the frame (i.e.
   right before text styles draw — confirm via `Style::compare`/`blend_order`
   (`style.h:221`, `sceneLoader.cpp:978-983`) that text styles do in fact already sort
   last; flag it if not, since the whole approach depends on it), blit/copy the default
   framebuffer's current color contents into the new offscreen buffer
   (`glBlitFramebuffer`, available given confirmed GLES3/`#version 300 es` usage).
3. Bind that texture as a new `u_backgroundTex` sampler, added to `TextStyle`'s
   `UniformBlock` (`textStyle.h:67-73`) and set in `onBeginDrawFrame`
   (`textStyle.cpp:72-104`).
4. In `sdf.fs`'s stroke/outline pass (the `u_pass == 1` branch driven by `text.vs:63-75`
   setting `v_sdf_threshold`/`v_color` from `a_stroke`): sample
   `texture2D(u_backgroundTex, gl_FragCoord.xy / u_resolution)`, compute luminance
   (`dot(rgb, vec3(0.299, 0.587, 0.114))` or similar), and scale the stroke pass's alpha
   by something like `smoothstep(lightThreshold, darkThreshold, luminance)` inverted so
   it's ~1 over dark backgrounds and ~0 over light ones — expose the two thresholds as
   `gui_variables`-style tunables the same way `u_texture_shading_*` are, so Sebastian
   can tune the falloff visually rather than guessing exact numbers.
5. The fill pass (`u_pass == 0`) is untouched — only the halo/stroke pass reacts to
   background luminance.

**Acceptance criteria:**
- `make -f tests.mk` passes; no GL errors in app log (check existing error-checking
  utility per `renderState.cpp`/`gl.h`).
- Headless screenshot: black peak-label text over snow (light background) shows little
  to no white halo; the same text over dark forest/water/shadow shows a clearly visible
  halo. Capture both in one frame if possible (a peak label whose halo visibly varies in
  strength along its own extent, if partially over a shadow edge) as the clearest
  demonstration this is truly per-pixel, not per-label.
- Rough perf sanity check: log frame time with the feature on vs. off over the same
  camera pose; flag if the extra blit is not clearly cheap relative to the
  hillshade/unlit-polygons GPU cost already dominating frame time (per the ongoing
  profiling work) — do not attempt to optimize further here, just report the number.

## Phase 5 — Curved lake & area/park labels

**Objective:** elongated water bodies and parks/protected areas get a classic curved
label following their principal axis; round/compact ones keep today's centroid point
label.

**Files:** `tangram-es/core/src/util/geom.h/.cpp` (new principal-axis/elongation
utility), `tangram-es/core/src/style/textStyleBuilder.cpp` (polygon-label branch,
`~line 290-298`, and the existing curved-line-label machinery at `~line 402-583` for
`CurvedLabel` construction to mirror), `assets/scenes/stylus-osm.yaml` (`water`/
`water_name` ~line 600-649, `landuse`/`national_park` ~line 1509-1532).

**Design:**
1. New geometry utility: given a polygon (outer ring, `vector<glm::vec2>`), compute its
   principal axis (area-weighted covariance matrix of the interior, not just boundary
   vertices, for robustness against dense vertex clusters — eigenvector of the larger
   eigenvalue) and an elongation ratio (major/minor eigenvalue ratio, or major/minor OBB
   extent ratio — pick whichever is simpler to compute robustly and document which).
2. In `TextStyleBuilder`'s polygon branch: if elongation is below a threshold (tune by
   eye, start around 1.4-1.6), keep today's centroid `Label::Type::point` unchanged. If
   above threshold, construct a line through the centroid along the principal axis,
   clipped to the polygon boundary at both ends (ray-polygon intersection), trimmed
   inward by a small margin (don't let text touch the shoreline/boundary), and build a
   `CurvedLabel` from that polyline exactly the way `addCurvedTextLabels`
   (`textStyleBuilder.cpp:402-583`) already does for line geometry — reuse that
   construction path/helper rather than duplicating it.
3. **Water labels**: today's `water_name` styling reads a separate pre-baked point layer
   (`$geometry: point`, `stylus-osm.yaml:626-649`), not the polygon. Change this to read
   the actual `water` polygon layer's geometry (already present in the same tile via the
   `water` fill layer, `stylus-osm.yaml:600-624`) so the shape is available to curve
   against — keep the existing point-layer path as a fallback for zoom levels or
   multipolygon shapes where this isn't attempted (e.g. below the elongation threshold,
   or multi-ring/complex geometries you choose not to handle). This is purely a
   client-side (tangram-es) change — no tilemaker/tile-schema change needed, the polygon
   data already ships in the vector tile.
4. **Parks/protected areas**: apply the same polygon-branch logic to the `landuse`/
   `national_park` draw group (`stylus-osm.yaml:1509-1532`) — no separate point layer
   involved there already, so this is a more direct application.

**Acceptance criteria:**
- `make -f tests.mk` passes; add a unit test for the principal-axis/elongation utility
  against a few synthetic polygons (a long thin rectangle, a near-circle, an L-shape)
  asserting sane axis direction and elongation classification.
- Headless screenshot: an elongated real lake (pick one with a clear long axis) shows
  its name curved along that axis instead of sitting at a single point; a round pond
  stays as today's point label. Same comparison for one elongated national park/forest.

## Phase 6 — Integration

**Steps:**
1. Merge phases in order 1, 4, 5 (independent of the texture-shading dependency,
   branched off `master`), then 2, 3 (branched off `master` post-merge, since Phase 1's
   merge, per this plan's design, happens on `master` and Phase 2/3 no longer need the
   separate `texture-shading-phase4-integration` branch — that work is already on
   `master` as of 2026-07-12). Expect overlapping edits in `stylus-osm.yaml` (peak/water/
   landuse draw groups touched by Phases 1, 2, 3, 5) and `textStyleBuilder.cpp` (touched
   by Phases 2, 3, 5) — resolve directly, they're mostly additive to different parts of
   the same rules.
2. Enable everything together, run `make -f tests.mk`, full Release build.
3. Headless screenshot smoke test (existing testenv pattern) over: (a) a peak-heavy
   mountainous area, (b) a lake/park-heavy area. Capture before/after for each phase's
   specific claim (font, priority reordering, anchor placement, halo, curved labels)
   where feasible in the same shots.
4. Single commit (or small series) on a `label-placement-phase6-integration` branch.
   **Do not merge to `master` or push to any remote — stop and hand back for human
   review**, per this project's standing git safety rules.

**Actually done (2026-07-12):** all 5 phase branches merged cleanly (submodule and outer
repo), one real conflict resolved (`util/textureShading.{h,cpp}` add/add between Phase 2's
real implementation and Phase 3's explicit stub — kept Phase 2's, and removed Phase 3's
texture-shading anchor-cost term entirely rather than wiring it up, since it runs on a
TileWorker thread where the sampler is provably unsafe to call — see
`anchorCandidateCost()`'s comment). `make -f tests.mk` (2015 assertions/182 cases) and full
Release build passed after every merge step.

**One real bug found by the headless smoke test that unit tests could not catch** (they
don't compile real GLSL): Phase 4's halo uniforms (`u_halo_luminance_dark`/`_light`) were
declared via the YAML `styles: text: shaders: uniforms:` mechanism, which only configures
*one* `Style` object per name. `sdf.fs` is shared by several distinct `TextStyle`-derived
objects beyond the "text" built-in — "contour-labels" (a separate built-in), plus an
implicit companion `TextStyle` created per point style that has a linked text child
(confirmed via targeted logging: "points", "poi-points", "track-markers", "loc-points" each
silently build a second, sdf.fs-based shader alongside their point.fs one). Every one of
those needed the same declaration, and the YAML mechanism can't reach them all. Fixed by
hardcoding both uniforms directly in `sdf.fs` (like `u_background_tex`) and setting their
values unconditionally in `TextStyle::onBeginDrawFrame` — trading away live GUI-slider
tuning (removed) for guaranteed correctness across every style instance. Worth remembering
for any future per-style YAML shader config targeting a built-in name: check whether that
built-in's shader source is shared by other Style objects first.

Two font files (`IBMPlexSans_Condensed-SemiBold.ttf`, `IBMPlexSerif-Italic.otf`) are
present in every worktree used this session but are **not committed anywhere** — see Phase
1's report: `assets/shared` is itself a submodule (`pbsurf/maps-res`) that's untracked-file
-ignored, so these need to be pushed there separately before this branch is usable outside
worktrees that already have them copied in.

Final headless screenshot (`docs/label-placement-plan.md` Phase 6, South-Coast BC/Howe
Sound area) confirmed clean rendering with no shader/GL errors — full visual sign-off on
font sizing, halo thresholds, prominence ranking, anchor placement, and curve quality is
still Sebastian's to do (per Phase 5 of `texture-shading-plan.md`'s precedent, this is not
an agent judgment call). Not merged to `master`, not pushed.

## Phase 7 — Four follow-up items (2026-07-12 through 2026-07-15, on `master` directly)

Implemented directly on `master` (per this round's instructions, no worktree/branch), one
commit per logical change in both the outer repo and the `tangram-es` submodule. All four
items below are done and committed; `make -f tests.mk` (2025 assertions/184 cases) and full
`make` pass after every commit.

### 1. Elevation number in regular weight

The text pipeline has no per-line/per-glyph-run font override anywhere (confirmed:
`FontContext::layoutText` shapes an entire label's text, including embedded `\n`, with one
`alfons::Font`; `TextStyle::Parameters`/`DrawRuleData::params` hold exactly one font per
rule) — so the original single combined `"name\nele"` string could only ever render in one
weight. Fixed by splitting into two separate `TextLabel`s, both linked to the peak icon via
`Label::setRelative()` (the same mechanism `PointStyleBuilder` already uses for icon+text) —
siblings of one another, not a 3-level icon→name→elevation chain, since
`TextLabel::applyAnchor()` only ever resolves one level of `m_relative`. New machinery: a
`text2_*` style param family (`text2_source`/`text2_font_family`/`text2_font_weight`/
`text2_font_size`) and `TextStyleBuilder::applySecondaryTextRule()`/
`PointStyleBuilder::addFeature()` support for building the second label. The elevation label
is pinned to a fixed `bottom` anchor (not independently salience-ordered) and pushed
straight down by the *primary label's actual measured height* plus a small gap.

Two real bugs found and fixed via headless testing, both worth remembering:
- `StyleContext::evalStyle()`'s JS-function string-result dispatch special-cases
  `text_source`/`text_source_left`/`text_source_right` to store the JS return value as
  literal text; any other key falls through to `StyleParam::parseString()`, which for
  text-source-shaped keys treats the string as a *feature property name to look up*, not
  literal text. `text2_source` was missing from that list — every secondary label silently
  rendered as empty text (a doomed `_props.getAsString("1548")` lookup) until this was added
  to both the string- and number-result switches.
- The push distance separating the two labels used *half* the primary label's measured
  extent instead of the full extent: `TextLabel::applyAnchor()` already centers a label a
  half-extent beyond its relative, so two same-relative siblings with no push start at the
  exact same near edge and fully overlap; clearing the primary's far edge needs the full
  extent, not half.

Verified via headless screenshots (temporary "E" prefix marker to unambiguously distinguish
the sub-label from any primary-label elevation-only fallback) showing the sub-label
correctly stacked below several real peak names in cached Lions/Cypress-area terrain.
**Needs Sebastian's visual sign-off**: final weight/size (currently 400/9px vs. the name's
600/12px — the 12px reflects a live edit Sebastian made to the scene file while this was
being debugged, not this session's original 9.5px).

### 2. Circular dot instead of triangle for peak icons

`assets/scenes/img/pois.svg`'s `peak` sprite: triangle path replaced with `<circle r="9"/>`
in the sprite's `-25..25` viewBox, matching the plain-dot style already used elsewhere in
the same file (e.g. `capital`). Screenshot-checked against the `14px` `points:` size on the
peak draw rule; radius picked by eye, **needs Sebastian's sign-off** like any other icon
sizing call in this project.

### 3. Sufficiently prominent peaks stay visible below z12 and near neighbors

Two structural gaps, both real (not threshold tweaks):

- **The peak filter's z12 exemption couldn't see the texture-shading prominence proxy**
  (only real OSM `prominence`/`wikipedia` tags, both synchronously available at tile-build
  time). Added a cheap, synchronous stand-in as a third `any:` branch: a JS predicate
  `feature.ele >= global.peak_prominence_ele_min` (default 2200m). This only affects
  *candidacy* — a peak that clears this bar still goes through the normal priority
  (`priority_texture_shading`, Phase 2) and collision pipeline; it just isn't permanently
  excluded from ever becoming a candidate below z12. `peak_prominence_ele_min` is exposed
  live via `gui_variables` (numeric spinner in the app's Map Variables panel, using the
  existing `min`/`max`/`step` → `createTextSpinBox` path in
  `MapsSources::populateSceneVars`, which already supports numeric globals alongside the
  boolean-checkbox and per-style-shader-uniform cases it was previously used for) — changing
  it calls `map->updateGlobals(...)` and rebuilds tiles, exactly like the existing
  `show_trails`-style boolean toggles.
- **`LabelCollider::process()` (tile-build time, `TileWorker` thread) permanently kills
  same-repeatGroup peak icons within `repeat_distance` of each other using *provisional*
  (pre-refinement) priority** — pure elevation for peaks without a real prominence tag,
  since `Label::refinePriority()`'s texture-shading correction only runs later, on the main
  thread. All peaks in the `peak:` draw rule share one implicit `repeatGroup` (same rule →
  same `DrawRule::getParamSetHash()`) and a default `repeat_distance` of a full tile width —
  a much larger radius on screen than the visual spacing between distinct real summits, so a
  locally-prominent-but-lower peak could lose this early, permanent cut to a
  taller-but-unremarkable neighbor before texture-shading refinement ever got a chance.
  Fixed by explicitly setting `repeat_distance: 40px` on the peak `points:` draw rule — tight
  enough to still dedupe genuinely-coincident sprite clutter without catching pairs of real,
  distinct peaks. (The alternative sketched in the original prompt — deferring/re-running
  the repeat-group cut after refinement — would be a materially bigger change touching
  `LabelManager`'s main-thread pass; not attempted, since shrinking `repeat_distance` closes
  the practical gap without it.)

New unit test (`tests/unit/labelTests.cpp`, "LabelCollider repeat-group suppression radius
tracks repeatDistance") locks down that `LabelCollider::process()`'s normal per-tile code
path (not the separate `>4096`-labels pre-filter, which uses a different, `/10`-scaled
formula) really does use `Options::repeatDistance` directly as the suppression radius in
screen pixels — the invariant the `repeat_distance: 40px` fix relies on.

**No concrete real-world before/after pair found** (unlike Phase 2's "Zinc"/"East Zinc"
precedent) — this session's cached test terrain (Lions/Cypress/Enchantment area) didn't
surface an obvious close-together low-prominence/high-elevation pair to screenshot. The fix
is verified structurally (the new unit test) and via confirming the scene still loads and
renders correctly, not a visual A/B. **`peak_prominence_ele_min` (2200m) and
`repeat_distance` (40px) both need Sebastian's own visual judgment** — reasoned from typical
peak spacing/elevation in this dataset, not tuned against a specific screenshot comparison.

### 4. Labels default away from covering high-prominence ridges

The deferred Phase 3 item. Phase 3's `anchorCandidateCost()` (`textStyleBuilder.cpp`)
documented texture-shading-based ridge avoidance as architecturally impossible at
tile-build time (main-thread-only sampler, `TileWorker`-thread caller) and left it as a
permanent limitation, not a stub awaiting wiring. It turned out not to be permanent — the
same main-thread deferral Phase 2 already established for priority refinement applies just
as well to anchor choice.

New `Label::refineAnchor()`, called from `LabelManager::processLabelUpdate()` right next to
`refinePriority()`, same "retry every frame until the elevation mosaic is available, then do
it once" pattern (new `Label::m_anchorRefined` flag mirrors `m_prominenceRefined`). Samples
texture-shading at a **fixed 25m real-world radius** around each candidate anchor direction
— Phase 3's screen-space footprint math doesn't carry over here, since this runs at
whatever the *current* view zoom/tilt happens to be, not the tile's fixed build-time styling
zoom, so there's no single "footprint in world meters" conversion. Cost is distance from the
texture-shading formula's neutral value 0.5 (gentle slopes read near-neutral either
direction; ridges/canyons push away from it) — lower cost is gentler ground. If a materially
better anchor exists (margin: 0.08), it's rotated to the front of the anchor fallback list
and switched to immediately via `Label::setAnchorIndex(0)`; the normal per-frame
`LabelManager::handleOcclusions()` collision pass that runs right after re-validates the new
position exactly like any other anchor and falls back further via `Label::nextAnchor()` on
its own if it turns out to newly collide with something — **no changes needed to
`handleOcclusions()`/`nextAnchor()`**, matching Phase 3's own "if you find yourself modifying
`handleOcclusions`, stop and reconsider" guidance.

The switch-or-not decision is factored out into a pure, directly-unit-tested function,
`Label::pickBetterAnchorIndex()` (`tests/unit/labelTests.cpp`) — the sampling itself needs a
real `ElevationManager`/`RasterSource`, too heavy to construct in a unit test, the same
reasoning the Frozen Interface Contract above gives for keeping `sampleTextureShadingAtMosaic()`
separately testable from `ElevationManager`'s GL/render-state machinery.

New `text_anchor_texture_shading` style param (`text:anchor_texture_shading: true` in YAML)
opts a text label in; wired only onto the peak name label (not the Task 1 elevation
sub-label, which uses a fixed anchor for unrelated reasons — see Task 1 above).

**Verification is weaker than the other three items and should be treated as directional,
not conclusive**: confirmed the scene loads with no shader/GL/parse errors and a tilted 3D
headless screenshot over real steep terrain (Lions/Cypress-area ridges) shows peak labels
sitting in lighter, less-shaded ground rather than stamped across the dark ridge/drainage
fan nearby — but this is not a rigorous disable/enable A/B on the same peak (would need
another full build+screenshot cycle this session didn't have remaining budget for after the
Task 1 debugging took much longer than expected). **The 25m sampling radius and 0.08
minimum-improvement threshold need Sebastian's own visual judgment** — reasoned from typical
peak-label scale, not tuned against a specific before/after comparison.

## Phase 7 follow-up (2026-07-15): real bugs and a real redesign, from user review

Sebastian's review of Phase 7 found one real regression and two real design flaws. All
fixed/redesigned and committed directly to `master`; `make -f tests.mk` (2024 assertions/184
cases) and full `make` pass after every commit.

### Elevation number vanished (real bug, not Task 4)

Reported as "used to show, now it's gone." Root cause had nothing to do with Task 4 (the
anchor-refinement flag was a red herring during investigation): `TextStyleBuilder::
applySecondaryTextRule()` (Task 1) copies the primary (name) label's `Parameters` wholesale,
which includes its `repeatGroup`/`repeatDistance` — so the elevation sub-label was
inheriting the **exact same repeatGroup as its own name label** (and every other peak's name
label sharing the draw rule). `LabelManager::withinRepeatDistance()` checks proximity within
a repeatGroup with **no exemption for `isChild()`/`relative()`** (unlike the real per-anchor
collision loop, which does skip a label's own relative) — so whenever the name label placed
successfully nearby, which is by construction always, the elevation label got occluded
outright by the repeat-group check, before ever reaching real collision testing. This bug
existed since Task 1 shipped; it wasn't caught earlier because its symptom is
order/state-dependent (only manifests when the name label *also* successfully places in the
same frame — ad hoc testing had repeatedly observed the elevation label rendering alone,
which was actually the bug's *other* face: the name label losing an unrelated collision and
never reaching `m_repeatGroups`, masking the interaction). Fixed by explicitly zeroing
`repeatDistance`/`repeatGroup` on the elevation sub-label — it isn't a competing peak
candidate needing dedup against other same-rule labels, it's a fixed decoration of one
already-chosen peak. Verified: name and elevation now render together reliably across many
peaks in the same headless screenshot (confirmed via the new texture-shading debug view,
below — nearly every peak in frame now shows both lines).

### Prominence gating was still elevation-based (real design flaw)

The Task 3 write-up above says "genuinely prominent peak" but the actual mechanism used
elevation as the candidacy signal — precisely what Sebastian's review flagged as wrong (OSM
`prominence` tags are usually missing, and raw elevation says nothing about how salient/
spiky a peak looks; a sharp 800m peak can be far more visually prominent than a broad,
gentle 3000m shoulder). Two changes, both keeping the same hard architectural constraint
(the visibility filter runs on a TileWorker thread, before any tile has main-thread texture
shading access — this has not changed and cannot change without moving filtering off that
thread entirely, out of scope here):

- `global.peak_prominence_ele_min` → renamed `peak_candidacy_ele_floor` and reframed
  honestly: it is **not** a salience signal, only a candidate-pool-size cap (every real peak
  on Earth would otherwise qualify as a below-z12 candidate). Lowered 2200m → 500m so it
  essentially never excludes a real candidate.
- The peak `priority:` function's no-real-tag branch no longer weights by elevation at all.
  It now returns a fixed, deliberately worst-in-tier provisional priority
  (`global.priority.peak + 0.999`) so an unconfirmed peak displays **nothing** until
  `Label::refinePriority()` confirms real texture-shading prominence on the main thread —
  literally "not placing labels until texture shading is computed," per the request. `0.999`
  (not some larger offset) deliberately preserves `floor(priority) == global.priority.peak`,
  since `refinePriority()` keeps that floor when resetting the fractional part to the
  texture-shading value — a larger offset would have moved the peak into a different integer
  priority tier and never let it become competitive again after refinement.

New: a raw texture-shading **debug visualization** (`u_texture_shading_debug`,
`hillshade.yaml`, live toggle via `gui_variables` → "Texture Shading Debug View") that
replaces the entire hillshade composite with the unblended `ts_shade` value as flat
grayscale — the literal same CPU-sampled signal driving peak priority and anchor placement,
so it can be visually cross-checked against which peaks actually get labeled. Verified via a
forced-on headless screenshot: the underlying relief structure is clearly visible and
peaks with real ridge structure are what's driving the composite, not a flat elevation
ranking.

### Ridge-avoidance redesign, informed by real cartographic literature

The original `Label::refineAnchor()` (25m fixed real-world sampling radius, single point per
compass direction, texture-shading only) was reviewed and found wanting: "the integral of
the texture-shading intensity AND feature occlusion (trails, POIs) that would result from
placing the label, given its size and shape, at each anchor" is the actual question, and the
default anchor order should follow established cartographic point-label placement priority
(Imhof), not an ad hoc list. A research pass (see below) confirmed the fix direction before
implementing it, rather than guessing.

**Research findings** (full agent report retained in this session's transcript, summarized
here): the numeric 8-position point-label ranking widely cited as "Imhof's rule" is not
directly from Imhof (whose 1962/1975 treatment is descriptive, not a numeric ranking) — it
was formalized by **Yoeli, P. (1972), "The Logic of Automated Map Lettering," *The
Cartographic Journal* 9(2):99–108**, and adopted as the objective function in
**Christensen, Marks & Shieber, "An Empirical Study of Algorithms for Point-Feature Label
Placement," *ACM ToG* 14(3), 1995**. Ranking, best to worst: **upper-right, upper-left,
lower-left, lower-right, right, top, left, bottom** — diagonal quadrants beat axis-centered
sides, and top-right leads because Latin-script ascenders/reading order make a label
above-right of a point symbol read as unambiguously attached to it. Neither CMS nor Yoeli
address scoring against a busy/detailed basemap (their objective is label-vs-label and
label-vs-point rectangle overlap only, no terrain/relief term). Two other papers give
concrete, cheap-approximation patterns directly applicable here: **Luboschik, Schumann &
Cords, "Particle-Based Labeling," IEEE TVCG 2008** (score obstacles via sample points along
their outline/contour, not a full-area integral) and **Kittivorawong et al., "Fast and
Flexible Overlap Detection for Chart Labeling with Occupancy Bitmap," VIS 2021** (rasterize
into a binary occupancy bitmap, test candidate footprints via O(1) bitwise ops per row).
Actionable synthesis used below: start from the Yoeli/CMS base rank, score relief and vector
density by sampling a handful of points on each candidate footprint's perimeter/corners
(not a full integral), combine as a weighted sum.

**Implementation** (`Label::refineAnchor()`, `tangram-es/core/src/labels/label.cpp`):
- Samples **both** texture-shading ridge cost and vector-feature density (via the tile's
  `AnchorOccupancyGrid` — the same structure Phase 3's tile-build-time
  `anchorCandidateCost()` already uses) at 5 points spread across each candidate anchor's
  **actual footprint** (icon radius + this label's own measured `dimension()`), mirroring
  `anchorCandidateCost()`'s own sampling pattern exactly. Footprint pixel offsets convert to
  the tile's normalized `[0,1]` local space via `MapProjection::tileSize()` — a fixed
  per-tile-geometry constant, not the current view's screen projection; valid to reuse here
  because this is about relating a fixed-size label footprint to its own tile's coordinate
  system, not to the current camera's zoom/tilt.
- The two cost terms are combined as an equal-weighted sum (tune by eye if one term should
  dominate) and the full anchor list is re-sorted by ascending combined cost — not just
  "switch if meaningfully better than current," which the original version did. Since this
  only ever runs once per label (`m_anchorRefined` guards further calls), there's no
  per-frame flicker/churn risk to guard against, so a clean full sort is simpler and more
  literally "sort anchors accordingly" (the actual request).
- `TextStyleBuilder::applyRule()`'s default anchor fallback order (used when a draw rule has
  no explicit `text_anchor`) changed from the ad hoc `{bottom, top, right, left}` to the
  Yoeli/CMS 8-position ranking. This is only the *starting* order — both
  `salienceOrderedAnchors()` (tile-build time) and `refineAnchor()` (main-thread) still
  re-sort it per-label; it only matters as the base/tie-break order when neither refinement
  runs (e.g. no terrain data for this map style).
- The switch-decision pure helper (`Label::pickBetterAnchorIndex`) is replaced by
  `Label::sortAnchorIndicesByCost()`, still directly unit-tested (stable-sorts a hand-built
  cost array) without needing a real `ElevationManager`/`RasterSource`.

**Not attempted, deliberately out of scope for this pass**: a true occupancy-bitmap /
particle-contour implementation (Kittivorawong/Luboschik's actual techniques) — the
5-point-footprint-sample approach already mirrors Phase 3's own established pattern and
keeps the change bounded; revisit if the coarse 5-point sampling proves visually
insufficient. Also not attempted: reading Imhof's actual 1962/1975 papers directly (not
freely available online in this session; relied on the well-documented Yoeli/CMS
formalization instead, which is explicitly presented in the literature as capturing Imhof's
intent numerically).

**Tunables needing Sebastian's own visual judgment**: the equal 1:1 weighting between
texture-shading cost and vector-density cost in `refineAnchor()`'s combined score (no
principled reason it should be exactly equal, just a reasonable starting point); the 5-point
footprint sampling pattern (vs. more samples, or a genuinely different scoring approach) if
it doesn't look right in practice; `peak_candidacy_ele_floor` (500m) and the `0.999`
worst-in-tier provisional priority fraction, both structural/architectural choices this time
rather than tuned constants, but still worth a visual sanity check.

## Phase 7 follow-up round 2 (2026-07-15): elevation label layout, per further review

Two more concrete pieces of feedback: (a) "the labels are too far away from the peaks...
should be half of their current distance," (b) "the elevation number is now included but
anchored separately. This makes no sense" -- with a specific requested layout: elevation
directly below the name (like a line break), left-aligned when the name sits right of the
peak, right-aligned when left of it, centered when the name is centered above/below the dot.

**Gap halved**: new `Label::Options::anchorGapScale` (default `1.0`, unchanged everywhere
else) scales the relative's dimension contribution to `TextLabel::applyAnchor()`'s
icon<->label gap formula. New `text_anchor_gap_scale` style param; the peak name label sets
it to `0.5`.

**Elevation label re-architected**: its `relative` is now the **primary (name) label**, not
the icon -- genuinely different from the original sibling-of-icon design. With a single
fixed `bottom` anchor and its own small `anchorGapScale` (`0.3`), the *existing* single-level
relative-dimension offset in `applyAnchor()` naturally stacks it just below the name in the
name's own local frame -- whichever screen direction that ends up pointing (right/left/top/
bottom) falls out for free, with no per-anchor-direction special-casing needed: this covers
all four cases in the request (right/left-anchored name -> elevation directly below;
top-anchored name -> elevation lands between name and icon; bottom-anchored name -> elevation
lands below the name) via one mechanism. Horizontal alignment is kept in sync every frame by
a new `Label::syncRelativeAnchor()` (mirroring `syncRelativePriority()`'s pattern exactly),
since the name's own anchor can still change later via `Label::refineAnchor()` running on the
main thread after tile build -- a one-time alignment decision at construction would go stale.

**One real bug found via screenshot verification**: the initial alignment formula had the
correction sign backwards, pushing the elevation number further from the intended edge
instead of toward it -- caught by a headless screenshot showing "1654" shifted the wrong way
under "The West Lion," fixed and re-verified in a follow-up screenshot. Worth remembering:
this class of sign error is exactly why the screenshot-verification step exists, not just the
unit tests -- the math type-checked and built cleanly, and no unit test caught it since the
formula's *correctness* (not just its shape) was the bug.

## Phase 7 follow-up round 3 (2026-07-16): independent placement, per further review

Sebastian caught a real crash: shortly after round 2 landed, he independently found and
fixed a SIGSEGV in `nextAnchor()` (garbage anchor-list count, e.g. `7106415`) via headless
Xvfb + `coredumpctl` (`04dca089d` in the submodule, `5c21a7e`/`1b26740` in the outer repo --
all authored by Sebastian directly, not this session). His fix (bounds-checked
`Anchors::operator[]`, an early-return guard in `nextAnchor()`) is a real safety net but
explicitly left the root cause open. Root cause not conclusively found, but round 2's
design had a real risk this round removes: the elevation label's `relative` pointed
directly at the primary (name) label instead of the shared icon -- every other label
relationship in this engine (icon<->text) keeps `relative` pointed at something with a
stable, well-understood lifetime; making a *sibling* label's `relative` point at another
sibling introduced a new pattern whose safety against the sibling dying independently was
reasoned through, not proven.

Also, real design feedback: real topo maps (Swisstopo, Kompass) and OSM itself don't
require a peak's name and elevation to be adjacent -- OSM commonly has an elevation without
a name (already handled: falls back to showing the elevation alone) or a name without
elevation data (currently excluded entirely by this draw rule's `ele: {min: 1}` filter, a
separate, un-addressed gap). Direction: place both independently via the same
Imhof/Yoeli-ranked, ridge/vector-avoidance-modulated selection, prioritizing the name's
placement, with "elevation stacks under the name" as a preferred *fallback* the optimizer
settles into only when it isn't a worse choice -- not a hard link.

**Redesign**: `Label::refineAnchor()` gains `setStackFallbackTarget()` -- a persistent
pointer, but read only ONCE, synchronously, inside `refineAnchor()`, never dereferenced
again afterward (unlike `m_relative`, read every frame for the label's whole lifetime).
`refineAnchor()` scores "stack directly under the target" as one additional candidate
(ridge + vector cost, same sampling machinery as the 8 compass anchors) and only takes it
over the best independent compass anchor when it isn't meaningfully worse
(`kStackPreferenceMargin`, tune by eye). Both labels keep `relative` = the icon throughout.
The elevation label now gets the full Imhof/Yoeli anchor list and the same
`salienceOrderedAnchors()` tile-build-time pass as the name (previously pinned to a single
fixed anchor). `anchorGapScale` unified to `0.25` for both labels (previously `0.5` for the
name and a compounded, inconsistent distance for elevation) -- addresses feedback that the
name was roughly 2x too far from the peak and elevation roughly 3x too far, and that the two
should match when placed independently.

**Verification note**: per explicit instruction, this round was NOT verified via headless
screenshot -- build (`make`) and unit tests (`make -f tests.mk`, 2024 assertions/184 cases)
only. Visual correctness (does the elevation actually land under the name when expected,
does independent placement look reasonable when it doesn't) is Sebastian's to check.

## Phase 7 follow-up round 3 addendum (2026-07-15/16): actual root cause found and fixed

Sebastian reported "peak labels (names and elevations) aren't showing at all anymore"
after round 3 landed -- worse than the earlier SIGSEGV symptom. This session root-caused
and fixed it directly (`954fe15d3` in the submodule, `8b950a3` in the outer repo,
including the headless verification below -- corrected here after an earlier draft of
this doc mistakenly attributed that work to Sebastian himself; the commit author field
just carries his configured git identity, not who actually wrote/ran it): a **real
use-after-free**, not just the uninitialized-looking garbage the earlier defensive fix
(`04dca089d`/`5c21a7e`, which *was* Sebastian's own direct fix) had guarded against.
`TextStyleBuilder::build()` drops and destroys dead labels (`unique_ptr` goes out of
scope), but nothing invalidated any other label's `m_stackFallbackTarget` still pointing
at one being dropped -- and this is a routinely-reachable path: the elevation sub-label
(`optional: true`, opted out of `repeatGroup`) frequently survives a tile-build-time
repeat-group/priority cut that kills its stack target (the name label) in the very same
pass. `Label::refineAnchor()` then dereferenced the dangling pointer on a later frame once
the elevation mosaic loaded, reading freed/reused memory -- explaining both the original
SIGSEGV and, once that crash was made non-fatal, the totally-blank-peak-labels symptom
(corrupted state rather than a clean early return).

Fix: `Label::clearStackFallbackTargetIfEquals()`, called from `TextStyleBuilder::build()`
for every surviving label against every label about to be dropped, right before the drop
(O(n) scan per dead label, tile-build time only, negligible for a tile's small label
count). Verified via repeated headless launches: zero out-of-range warnings (previously
non-zero, different garbage every run) and zero crashes, correct rendering --
`coredumpctl` independently confirms real SIGSEGV/SIGABRT crashes in `build/Release/ascend`
on 2026-07-15 (20:44-20:51) before this fix, none since.

Both `Anchors::operator[]`'s bounds check and `nextAnchor()`'s empty-list guard
(Sebastian's earlier defensive fix) are kept as defense-in-depth, even though the actual
root cause turned out to be this dangling pointer rather than a genuinely uninitialized
`Options.anchors`.

**This was not the whole story.** Sebastian's next report -- "many peaks aren't showing at
all unless super zoomed in, even if there are no other peaks in the area" -- ruled out
collision/clutter as the cause (no competing peaks) and pointed straight at a filter/zoom
gate instead. Found it: the `peak:` draw rule's filter had a second, older `all:` clause,
`[ any: [{$zoom:{min:17}}, global.show_trails], any: [{$zoom:{min:15}}, {name:true}] ]` --
two sibling `any:` blocks under `all:`, i.e. ANDed, not the single OR the adjacent comment
("named peaks from z15, all from z17") actually described. Expanding the boolean algebra:
`(zoom>=17 OR show_trails) AND (zoom>=15 OR name)` reduces to `zoom>=17 OR (show_trails AND
(zoom>=15 OR name))`. `global.show_trails` defaults to `false`, so on the default (non-Hike)
map style this was just `zoom>=17`, full stop -- regardless of name, prominence, or how
empty the surrounding area was. This gate predates the texture-shading candidacy/priority
system built this week (a leftover simple zoom-tier heuristic from before it existed) and
silently defeated the entire point of that system: peaks are already gated by the OR
candidacy filter above and by priority+collision refined from real texture-shading
prominence -- the rule's own comment says that's meant to be the *sole* salience gate.
Every verification screenshot taken during this whole multi-round effort (this session's
and the round-3 addendum above) used `stylus-bike-hike` as the test config's last-used map
source, which force-sets `global.show_trails: true` -- masking this bug completely the
entire time. Confirmed with a same-view, same-zoom before/after headless screenshot pair
switching only the map source: 5+ named peaks with elevations under `stylus-bike-hike`,
zero peaks of any kind under the default `stylus-osm-terrain` source; after removing the
gate (`932194d`), peaks render correctly under the default source too. Scene-YAML-only
change, `make -f tests.mk` unaffected (2024 assertions/184 cases, still passing).

One residual, separate, lower-severity anomaly noticed while fixing this: in extremely
cluttered terrain (tested: Zermatt/Matterhorn at z11-12.5), some very prominent named
peaks -- the Matterhorn itself, specifically -- still don't get a name label even once
this gate is removed, while nearby less-famous peaks do. Likely an ordinary
priority/collision loss in a uniquely cluttered spot (dense hut/piste/trail labels right
at that location) rather than a new instance of this bug, but not yet root-caused --
worth a follow-up look if Sebastian still sees specific well-known peaks missing after
this fix.

**Status**: peak name/elevation placement (independent siblings of the icon, Imhof/Yoeli-
ranked anchors, ridge+vector-aware `refineAnchor()`, stack-under-name preferred fallback)
is implemented, builds clean, passes all unit tests, and headless verification (multiple
regions, multiple zooms, default map source) shows it rendering correctly with no crashes
and no more zoom-gate suppression. Still outstanding, not yet addressed: named peaks with
no elevation data are excluded entirely by the draw rule's `ele: {min: 1}` filter (flagged
in round 3, not fixed); the Matterhorn-specific anomaly noted just above; the equal 1:1
texture-shading/vector-density cost weighting and the 5-point footprint sampling density
remain unvalidated tunables pending Sebastian's own visual judgment on the live map.

## Phase 7 follow-up round 4 (2026-07-16): named vs. unnamed peaks, and a prominence question

Sebastian's next report, in one message: (a) unnamed elevation-only peaks were rendering
their number in the primary label's big/semibold font, indistinguishable from a real peak
name -- "someone put them in as peaks with the elevation as the name in OSM? ... or is
this a bug" (it was our own fallback, not OSM data); (b) in cluttered terrain, notable
named peaks (Matterhorn specifically) should be prioritized over anonymous elevation
points based on "prominence, height, and the fact that they have a name"; (c) an open
question about whether the texture-shading signal is theoretically adequate as a
prominence proxy, or whether something scale-aware is needed.

**(a)+(b) fixed** -- see `tangram-es` commit `94bae6955` / outer `83863b4`. Root cause of
both: named and unnamed peaks shared one priority band (worst-in-tier until
texture-shading refinement), and the primary label's `text_source` used the elevation as
a name-fallback in its own 12px/600 font. Now: two disjoint half-integer priority bands
(named `[peak, +0.4]`, unnamed `[peak+0.5, +0.9]`, blending in real OSM `prominence` or a
weak height-based tie-break per the user's "prominence, height, and name" framing --
`Label::refinePriority()` recovers the band via `floor(priority*2)/2` instead of
`floor(priority)`, which is what actually survives refinement instead of collapsing both
bands together); primary `text_source` returns `""` with no name, and text2 (small 9px
font) is now built independently of whether the primary succeeded, so it's unconditionally
the only place elevation ever renders. Verified via headless screenshot (default map
source): visibly more named peaks surface, and bare elevation numbers are now clearly
smaller than peak names. The Matterhorn itself still didn't get a label in the one
extremely cluttered Zermatt test view even after this fix -- likely an ordinary
collision loss in a uniquely busy spot (dense hut/piste/trail labels), not a repeat of
this bug; not yet root-caused, flagged for a follow-up look.

**(c) is a real, unresolved design question, analyzed but not implemented.** The CPU
sampler (`textureShading.cpp`) sums `kMaxLevels=4` box-filter-downsampled octave bands,
each a further 2x2 downsample of the last, weighted by Brown's `2^(-k*alpha)` power law --
this is already "multi-scale" in the sense the user describes, but the scale RANGE it
covers is small and fixed: each octave doubles the ground footprint from the tile's own
texel size, and `kMaxLevels` is capped at 4 specifically because the mosaic is only a
3x3-tile neighbor-ring (`docs/texture-shading-plan.md`'s Frozen Interface Contract) --
going deeper would start reading past real data into the mirror-extrapolated edge, giving
wrong answers. In practice this means the signal captures local curvature/roughness within
roughly a tile-texel-scale neighborhood (hundreds of meters, not tens of kilometers), and
is NOT tied to current view zoom at all (`sampleTextureShading()`'s own doc comment: fixed
alpha/contrast, unlike the live shader's continuous zoom-dependent blend -- deliberate, so
a peak's priority doesn't change as the camera moves).

This explains the Matterhorn case mechanically: real topographic prominence is about
standing out from an entire REGIONAL basin (tens of km), which is a fundamentally larger
scale than 4 local octaves can see. Locally, the Matterhorn's summit curvature isn't
necessarily sharper than Dent Blanche's or the Breithorn's -- they're all steep alpine
rock -- so a purely local signal can't tell "the one peak that dominates this whole
valley" from "one of several similarly jagged neighbors." True prominence (height above
the highest saddle connecting to any higher peak) is a global watershed computation, well
outside what a live, per-tile, streaming renderer can do -- it's exactly what OSM's
`prominence` tag is FOR, when present (which, per Sebastian's own observation, is rare).

A tractable middle ground, not yet built: a genuine large-radius "isolation" signal,
computed cheaply by ring-sampling the elevation raster (already fetched per-tile, no new
mosaic infrastructure needed) at a handful of points across a few increasingly large
radii, and comparing the peak's own height against the ring maxima -- a coarse
approximation of "is there anything higher nearby" rather than "is this local texture
sharp." The user's "relevant to the current zoom level" framing maps naturally onto this:
ring radius should grow as view zoom decreases (zoomed way out, only regionally-dominant
peaks should matter; zoomed in, local texture-shading sharpness is the more relevant
signal), blended with the existing local signal rather than replacing it. This is a real
follow-up worth scoping as its own phase -- new sampling infrastructure, a threading/perf
story (ring sampling at large radii means touching tiles beyond the current mosaic, so it
needs its own main-thread-safe lookup path, likely amortized/cached since it's much more
expensive than the existing single-tile mosaic lookup), and its own visual tuning pass --
not attempted in this round.

## Phase 7 follow-up round 5 (2026-07-16): icon gating, distance consistency, tile-build cut

Sebastian, one message, three issues:

1. "peak dots shouldn't be shown unless at least an elevation number is shown next to it,
   or an elevation AND a name." **Fixed** (`tangram-es` `c55d5c478` / outer `f375b91`). The
   icon<->text "die together" cascade (`LabelCollider::killOccludedLabels` /
   `LabelManager::handleOcclusions`, both keyed on `Options::optional`) had the gate on the
   wrong label: the name was non-optional (so its failure killed the icon), elevation was
   optional (so its failure didn't) -- meaning a dot could end up alone with just a number,
   with a name and no number, or with nothing at all. Flipped: `text:` (name) now sets
   `optional: true`, `text2` (elevation, `pointStyleBuilder.cpp`) is now the non-optional
   one. The icon strictly requires a successfully-placed elevation; the name is free extra
   context that can fail independently.

2. "the elevations are still too far away from the peak dots sometimes -- their distance
   seems to be sometimes very close, sometimes quite far? how is this possible?" **Fixed**,
   same commits. Root cause: the stack-fallback mechanism from round 3/4
   (`setStackFallbackTarget()`/`m_stackFallbackTarget`) computed the elevation's offset from
   the NAME's own dimension and position whenever stacking won out over an independent
   compass anchor -- so the icon-to-elevation distance varied with the name's text
   length/wrapping instead of being constant. Removed entirely (also closes off the second
   use-after-free source found this session, `clearStackFallbackTargetIfEquals()`): both
   labels now always use the identical icon-relative anchor formula (icon radius + own
   half-extent + `anchorGapScale` gap), so the distance is the same fixed value for every
   peak, always. "Stacked under the name" can still happen visually when both independently
   land on vertically-adjacent anchors, but it's no longer a distinct offset-computing mode.

3. "I see locations sometimes where a whole range doesn't show the label for the most
   prominent peak; it shows maybe just one for a small foothill, but nothing for the big
   peak until I really zoom in, with no other labels or trails interfering either." **Fixed**
   (outer `f375b91`, `repeat_distance: 40px` -> `0` on the peak icon). Root cause: this is
   the original Task 3 failure mode resurfacing -- `LabelCollider::process()` (tile-build
   time, `TileWorker` thread) applies repeat-group mutual suppression using only the
   PROVISIONAL priority (name-status + weak height tie-break, see round 4 above), since
   texture-shading refinement is main-thread-only and doesn't exist yet at this point. Any
   nonzero `repeat_distance` risked this crude provisional signal permanently killing
   (`Label::State::dead`, irreversible -- never reconsidered once real refinement would have
   ranked things correctly) the wrong one of two nearby real peaks. Worse at low zoom, where
   real-world peak-to-peak spacing maps to fewer screen pixels -- exactly matching "shows
   only once I zoom in a lot," and unrelated to actual label/trail clutter, matching "no
   other labels or trails interfering." Disabled entirely; deduplicating genuinely-coincident
   sprites (the same summit double-tagged in OSM) is left to the real per-frame OBB collision
   pass instead, which runs after refinement with the correct, true-prominence-ranked
   priority.

Verified via headless screenshot (default map source, multiple regions): every visible dot
now has at least an elevation number, name/elevation sit at a visually consistent distance
from the dot across many peaks, and noticeably more named peaks survive that previously
would have lost a tile-build-time repeat-group cut. `make -f tests.mk`: 2024 assertions/184
cases, all passing throughout.

## Phase 7 follow-up round 6 (2026-07-16): Debug-vs-Release placement divergence

Sebastian reported the Debug build (`build/Debug/ascend`) and Release build
(`build/Release/ascend`) show genuinely different peak labels at the same location/zoom --
concretely, Debug shows the Matterhorn's label in the cluttered Zermatt test view, Release
doesn't (matching the "Matterhorn-specific anomaly" flagged as unresolved in round 4/the
addendum above). Root-caused, not yet visually re-verified by Sebastian (see below).

**Root cause**: `LabelManager::priorityComparator()`'s final tiebreak (`labelManager.cpp`,
used to sort all labels by priority every frame before `handleOcclusions()` greedily assigns
screen space) fell through, when every other criterion tied, to `return l1 < l2;` -- comparing
the labels' raw heap addresses. Two same-style peak labels share one `hash()`
(`m_options.paramHash`, a style/DrawRule param hash -- not feature-specific, so *every* peak
label built from the one `peak:` draw rule collides here), so this address compare is the
practical, everyday tiebreak for exactly the "two nearby peaks, tied priority" case -- either
during the window before `Label::refinePriority()`'s texture-shading refinement completes (both
still share the identical worst-in-tier provisional value from round 4), or permanently, when
the texture-shading proxy genuinely can't separate two similarly rugged summits (round 4's own
analysis already concluded this is likely for Matterhorn vs. its neighbors -- a real local-signal
limitation, not a bug). Whichever label wins this tiebreak first also tends to keep winning
afterward via the `occludedLastFrame()` hysteresis a few lines up (explicitly commented as
"non-deterministic placement... depending on navigation history" -- an intentional
flicker-reduction tradeoff, not itself new).

Comparing raw addresses made that initial coin-flip **silently build-dependent**: `Label`'s
`debugTag` member (`label.h`) only exists `#ifdef DEBUG`, so `sizeof(Label)` -- and every
subclass built on it -- genuinely differs between Debug and Release, on top of the different
heap allocation patterns `-O0` vs `-O2` produce everywhere else in the frame. Same input,
same code, deterministically different tiebreak outcome per build.

**Fix**: added `Label::id()`, a monotonic creation-order serial (`static std::atomic<uint32_t>`
counter in `nextId()`, `label.h`), and changed the tiebreak to `l1->id() < l2->id()`. This
removes the build/allocator dependency entirely; two labels built on the same TileWorker thread
(the common case for two nearby peaks in the same or adjacent tile) now always tiebreak the same
way regardless of build type. Cross-thread nondeterminism (two competing labels built by
different tiles' worker threads completing in a different order) is a separate, harder,
pre-existing problem the `occludedLastFrame()` comment already flags -- not attempted here.

**Verification**: `make -f tests.mk` (2024 assertions/184 cases) and both `make DEBUG=0` and
`make DEBUG=1` build clean. Per updated project policy (see repo root `CLAUDE.md`), no headless
screenshot was taken -- this needs Sebastian's own visual check that Debug and Release now agree
at the same Zermatt view. Exact commands to reproduce, both same test view used throughout this
plan (see `CLAUDE.md` for the run-command template):

```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5
```

**Not addressed by this fix, still open** (Sebastian's other reported symptom, likely present in
both builds, not a Debug-vs-Release difference): peak dots showing with no visible label, or with
the elevation number placed far enough from the dot that they don't read as belonging together.
This needs its own investigation into `Label::refineAnchor()`/`anchorGapScale` -- not yet started.

## Phase 7 follow-up round 7 (2026-07-16): viewport-relative regional isolation

After round 6's fix, Sebastian confirmed Debug and Release now agree -- but the Matterhorn
label was missing in *both*, confirming round 4's own analysis: the local texture-shading
term genuinely can't separate "the one peak that dominates the whole valley" from "one of
several similarly jagged neighbors," so it isn't a coin-flip anymore, the coin just always
lands the same (wrong) way now.

The original plan (round 4's "tractable middle ground") sketched fixed-radius ring sampling
out to ~20km. Research into `RasterSource`/`ElevationManager` confirmed the *read* side is
cheap and already-safe (same cache-only contract as the existing sampler), but the *write*
side -- actively fetching coarse regional tiles for an arbitrary 20km radius around a peak
that may not otherwise be visible -- has no existing path reachable from `Label::refinePriority()`
without duplicating a chunk of `TileManager`'s fetch/dedup/backoff machinery (confirmed: the
local cache had zero low-zoom tiles anywhere near Zermatt, so a cache-only version would have
done nothing for the actual reported case).

**Sebastian's own reframing avoided all of that**: use only the CURRENT VIEWPORT as the
region, not a fixed real-world radius. This needs no new fetches at all -- elevation data for
whatever's currently on screen is already loaded for hillshade/terrain rendering -- and it
gives "relevant to current zoom level" for free: zoomed out, the viewport covers a huge
real-world area, so only a genuinely regionally-dominant peak scores well; zoomed in, it's a
small area and local peaks compete fairly. No explicit radius-vs-zoom formula needed, unlike
round 4's original sketch.

**Implementation, first attempt (superseded within this same round -- see the correction
below)**: `ElevationManager::getMinMaxElev(TileID, ancestors)` already existed
(`elevationManager.cpp`) -- cache-only, climbs toward coarser ancestor tiles until one is
resident, memoized per-texture. `Label::isolationAncestorZoom(viewZoom, viewportMaxPixels,
tileSize)` picked an ancestor zoom whose tile width roughly equals the viewport's larger
screen dimension (`view_zoom - log2(viewport_tiles_across)`), and `Label::refinePriority()`
computed that ancestor `TileID` at the peak's own world position and called `getMinMaxElev()`
directly.

**This didn't work.** Sebastian tested it: Matterhorn still showed only an elevation number,
no name, and loading the same view had become "absurdly slow." Investigating the slowness
(below) required real profiling, and while verifying the isolation term with the same
technique (temporary instrumented builds, `ASCEND_PROFILE_SECONDS`-gated `Profiler` capture,
`scripts/perf/summarize_trace.py`), a temporary hit/miss counter showed **0 hits, 900+ misses,
always** at the computed ancestor zoom (9, for this view). Root cause: elevation raster tiles
are only ever fetched matching the *vector* tile's own zoom (confirmed via the profiler's
`stopZoomByMaxZoom:elevation` counter, pinned at 12 for this z12.5 view) -- nothing in the
normal tile-loading pipeline ever independently loads a *coarser* elevation tile just because
a peak's isolation check wants one. The ancestor tile computed by `isolationAncestorZoom()`
was reliably never resident, so the entire isolation term had been a silent no-op the whole
time; every peak's priority was identical to round 6's fresh shade-only value, Matterhorn
included.

**Corrected design**: don't gamble on a speculative ancestor tile at all -- use the elevation
data that's *definitely* already resident, because it's needed for rendering the current
frame anyway: the currently-visible tiles' own (native-zoom) elevation textures.
`LabelManager::updateLabels()` now computes `viewportMaxElev` **once per frame** (not once per
peak) by calling `getMinMaxElev(tile->getID(), 0)` -- ancestors=0, so no climbing, just "is
this exact tile's own texture resident" -- for every tile in `_tiles` and taking the max
across all of them; a tile with nothing resident (flat style, terrain off, not yet loaded) is
skipped. This is both a correctness fix (data that's actually there) and a performance win
(one aggregation per frame instead of one attempted ancestor-lookup per peak).
`Label::refinePriority()`'s signature changed from taking `const ViewState&` (used only to
derive the now-deleted ancestor zoom) to taking the precomputed `float viewportMaxElev, bool
haveViewportMaxElev` directly; `isolationAncestorZoom()` and its unit tests were removed as
dead code. `Label::isolationScore()` (pure, unit-tested: 1.0 at/above the max, ramping to 0.0
over a tunable margin below it) is unchanged, now fed the real per-frame viewport max instead
of a phantom ancestor tile's. Verified via the same temporary hit/miss counter: 4 hits, 0
misses, real nonzero scores (e.g. `peakElev=4380 viewportMax=4613 isolation=0.223`) in the
Zermatt test view.

Blended additively with the existing `compressTextureShading()` term as originally planned
(`kIsolationWeight = 0.15`, tune by eye), clamped so the combined compression can't spill past
the half-tier band width. Best-effort throughout: if no visible tile has elevation data, or
this peak's own elevation isn't available, isolation contributes 0 -- never a hard requirement
gating finalization, unlike the shade sample. Evaluated once and latched
(`m_prominenceRefined`), same one-time-snapshot behavior as the shade term (Frozen Interface
Contract: priority must not change as the camera moves) -- so this peak's isolation score
reflects whatever the viewport's max elevation happened to be the frame refinement first
succeeded, not a live-updating value.

**Answering Sebastian's side question** ("maybe isolation already captures ridges being less
prominent"): no -- isolation and local shape (the existing texture-shading term) are
different, complementary signals. A ridge's topmost bump can be regionally dominant (nothing
else nearby is higher) while still reading as a flat/saddle-shaped ridge crest rather than a
convex spike in the local curvature term. Blending both (not replacing) means a peak needs to
*both* look locally spiky *and* actually stand out regionally to rank highest -- which is what
was already planned, not a new mechanism.

**Known limitations, not attempted**: if none of the currently-visible tiles have elevation
data resident on the exact frame a given peak's shade-refinement first succeeds, isolation is
permanently skipped for that peak (no separate retry flag was added, to avoid a third latch
mirroring `m_prominenceRefined`/`m_anchorRefined`) -- in practice this only matters very early
in a session, before any terrain tiles have loaded at all.

## Phase 7 follow-up round 8 (2026-07-16): the real "absurdly slow to load" bug

Investigating the slowness Sebastian reported (see round 7 above) required actual profiling,
not more speculation -- built a temporary env-var-gated hook
(`ASCEND_PROFILE_SECONDS`, `linuxmain.cpp`, reverted after use) to drive the existing
`Profiler`/`scripts/perf/summarize_trace.py` infrastructure headlessly, since the debug-menu
checkbox that normally starts a capture isn't reachable without clicking a real GUI. First
finding: a single frame took **25.5 seconds**, with `LabelsCollect` (the scope wrapping
`LabelManager::updateLabels()`, where all per-label refinement happens) accounting for 92%+ of
it. A/B test with round 7's isolation code fully disabled (`if (false)`) showed the exact same
stall persisted (44.8s that run) -- **not round 7's code**, a pre-existing bug, most likely
already present after round 6 too, just not something anyone had profiled at this specific
"extremely cluttered" Zermatt view (2659 peak labels active per the `labelsActive` counter --
far more than any earlier test location).

**Root cause**: `sampleTextureShadingAtMosaic()` (`textureShading.cpp`) decodes the whole
mosaic buffer to float and builds a `kMaxLevels`-deep box-filter pyramid from scratch on
*every single call* -- and its own comment already flagged this as a known, unimplemented gap
("this runs at most once per peak in the common case"). That assumption was wrong:
`Label::refineAnchor()` (Task 4, an earlier round) calls it up to ~45 times per peak (5 sample
points x up to 9 anchors), all against the identical mosaic. With ~2659 peaks each paying a
~35M-float-op rebuild up to 45 times, this is tens of billions of redundant operations in one
frame -- exactly the observed magnitude.

**Fix**: none of the pyramid-building work depends on the specific sample point (only on the
mosaic's own pixel data), so it's factored out into `buildPyramid()`/`sampleFromPyramid()`
(anonymous namespace, `textureShading.cpp`). `sampleTextureShadingAtMosaic()` (the Frozen
Interface Contract's pinned, directly-unit-tested signature) is unchanged in behavior --
still rebuilds every call, since its only callers are unit tests. The actual hot path,
`sampleTextureShading(RasterSource&, ...)`, gets a single-slot cache keyed by a
`std::weak_ptr<Texture>` (not a raw pointer -- a destroyed/replaced mosaic, e.g. from a fresh
stitch, must never be mistaken for a live one) so consecutive calls against the same mosaic
(the overwhelmingly common case: one peak's own ~46 calls, or several nearby peaks sharing one
tile) reuse the cached pyramid instead of rebuilding it.

**Result**: worst-case frame in the same Zermatt view dropped from ~25-45 seconds to ~5.7
seconds (with *more* peaks active this run, 2659 vs the earlier 527) -- roughly an order of
magnitude. The remaining cost is now comparable in magnitude to hillshade's GPU cost, which is
a separate, already-tracked issue (see the profiling-system memory/prior session's GPU
findings) -- not chased further here.

**Verification**: `make -f tests.mk` (2031 assertions/185 cases -- down slightly from round
7's count, from removing the now-dead `isolationAncestorZoom` tests) and both
`make DEBUG=0`/`make DEBUG=1` build clean. Profiling was done with temporary instrumentation
(env-var hook in `linuxmain.cpp`, ad hoc `PROFILE_SCOPE`s in `label.cpp`), all reverted before
finishing -- no permanent new tooling was added, per the "minimal diff" approach. Per
`CLAUDE.md` policy, no headless screenshot was taken for the label-placement question itself --
needs Sebastian's own visual check that the Matterhorn (and the East/West Lions, a second
real-world test case) now outrank non-dominant neighbors, and that loading feels reasonable.
Same run commands as round 6/7:

```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5
./build/Release/ascend --view.lat 49.38 --view.lng -123.20 --view.zoom 13
```

**Tunables needing Sebastian's own visual judgment**: `kIsolationWeight` (0.15) and
`kIsolationMarginMeters` (300m), both in `label.cpp` -- reasoned starting points, not tuned
against a specific screenshot comparison.

## Phase 7 follow-up round 9 (2026-07-16): loading still slow; the real Matterhorn story

Sebastian tested round 8's fix: still slow, Matterhorn still no name label. Both needed
another, harder look -- round 8's fix was real but insufficient, and round 7-9's isolation
work turned out to be solving the wrong problem for Matterhorn specifically.

### The real remaining perf bug: too much per-frame work, not just expensive-per-call work

Round 8 made each `sampleTextureShading()` call cheap (cached pyramid) but didn't reduce how
many calls happen. Re-profiled with real numbers: a Debug-build run at the same Zermatt view
hit a single frame that took **98 seconds** (`LabelsCollect` alone: 77s), confirmed via the
same temporary `ASCEND_PROFILE_SECONDS` profiler hook as round 8. With `labelsActive` up to
2659 in this cluttered view, and `Label::refineAnchor()` doing up to ~45 (now individually
cheap) samples per peak, the sheer count -- tens of thousands of calls converging in the same
frame -- was still enough to stall for many seconds (Release) to well over a minute (Debug,
`-O0`, no optimization at all).

**Fix**: spread the one-time refinement cost across multiple frames instead of doing it all
at once. New `LabelManager::m_anchorRefineBudget` (labelManager.h/.cpp), reset to
`kMaxAnchorRefinementsPerFrame = 150` (tune by eye) at the top of every `updateLabels()` call,
decremented each time `refineAnchor()` actually runs for a label; once exhausted, remaining
labels simply wait for a later frame (same "retry every frame until success" idiom
`m_anchorRefined` already used, just now frame-budget-gated too). Safety: when the budget is
exhausted while labels are still waiting, `m_needUpdate` is set so the app keeps requesting
frames until everything settles -- without this, a session that stops moving the camera right
as the budget runs out could leave labels permanently stuck unrefined.

**Result, re-profiled with the fix**: `LabelsCollect` no longer appears in the top zones by
time at all (down from 77s to well under 100ms aggregate across many frames in the same test).
The dominant remaining cost is now `style:hillshade`/`style-gpu:hillshade` -- GPU rendering,
not label refinement, and a separate, already-known issue from earlier profiling work, not
something this plan's work introduced or can fix here.

**Confound found while re-testing**: the app persists camera `rotation`/`tilt` across
sessions (`view.rotation`/`view.tilt` in config, same mechanism as `view.lng`/`lat`/`zoom`,
`mapsapp.cpp`). A leftover tilted/rotated camera state from earlier testing this session
(`rot=302.4deg tilt=53.1deg`) was inflating render cost and changing which area was actually
visible, confounding earlier comparisons. Pass `--view.rotation 0 --view.tilt 0` explicitly
for reproducible testing -- added to `CLAUDE.md`'s run-command template. With a clean
(untilted) camera, the same view's worst frame was 2.5s, entirely GPU/hillshade-bound, no
label cost at all.

### The real Matterhorn story: it already had real prominence data

All of rounds 7-9's isolation work is for peaks that **lack** a real OSM `prominence` tag --
Phase 2 (long before this session) already suppresses `refinePriorityWithTextureShading` for
any peak where `feature.prominence` is present, since the real tag drives a final priority
directly at tile-build time with no refinement needed at all. Decoded the actual vector tile
covering Zermatt (`assets/cache/stylus-osm.mbtiles`, z12/x2135/y2638, hand-rolled a minimal
protobuf/MVT parser since no Python MVT library was available) to check Matterhorn's real
tags directly rather than continuing to guess from a slow, unreliable live-Debug-build
diagnostic (which was taking minutes per attempt and repeatedly timing out on tile loading
in this sandboxed environment): **`osm_id=26863664`, `natural=peak`, `name=Matterhorn`,
`ele=4478`, `prominence=1038`.**

This means Matterhorn's priority has been driven by real, substantial, correct prominence
data the entire time, via a code path this session never touched. The isolation term (rounds
7-9) was never in the running to explain why it's missing a label -- it doesn't even run for
this peak. Whatever is actually suppressing Matterhorn's label is a **different** bug,
already flagged as an open anomaly back in round 4/the round-3-addendum ("some very prominent
named peaks -- the Matterhorn itself, specifically -- still don't get a name label... likely
an ordinary priority/collision loss in a uniquely cluttered spot... not yet root-caused") --
i.e. this predates this session's isolation work entirely and needs its own separate
investigation (most likely something about collision against nearby hut/piste/trail labels,
or a draw-tier/priority-comparison interaction between peaks and other label types, not
peak-vs-peak ranking). Not investigated further this round -- flagged for its own pass.

**Verification**: `make -f tests.mk` (2031 assertions/185 cases, unchanged from round 7/8 --
this round added no new pure-testable logic, only the frame-budget mechanism, which isn't
independently unit-testable without a real `LabelManager`/`Scene`) and both `make DEBUG=0`/
`make DEBUG=1` build clean. All temporary diagnostics (profiler hook, ad hoc `LOGW`s, the MVT
decode script) were removed/left outside the repo; nothing permanent added beyond the budget
mechanism itself. Updated run commands (now resetting rotation/tilt):

```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5 --view.rotation 0 --view.tilt 0
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5 --view.rotation 0 --view.tilt 0
```

**Tunable needing Sebastian's own visual/perf judgment**: `kMaxAnchorRefinementsPerFrame`
(150, `labelManager.cpp`) -- larger settles faster but risks longer per-frame stalls; smaller
is gentler per-frame but takes more frames (and hence longer wall-clock time while the camera
is still, before `m_needUpdate` stops mattering) to fully settle.

## Verification approach

- Each phase: project builds clean (`make`, Release), relevant unit tests pass
  (`make -f tests.mk`), plus a headless screenshot smoke test via the existing testenv
  pattern (`run_shot.sh`-style harness from prior sessions) for anything visual.
- Final integration: live-app screenshots over real areas, before/after comparison,
  hand to Sebastian for the actual visual judgment call (font sizing, halo darkness
  thresholds, curve quality, anchor placement quality) — do not self-grade aesthetics.
- No merge to `master`, no push, without explicit approval.

## Execution notes for agents

- Each phase agent works in an isolated git worktree, branching from `master` (Phases 1,
  4, 5) — Phases 2 and 3 also branch from `master`, since the texture-shading dependency
  they need is already merged there as of 2026-07-12 (see Background section).
- Read this entire file first. For Phases 2/3, also read `docs/texture-shading-plan.md`
  in full — its Frozen Interface Contract is a direct dependency of this plan's own
  Frozen Interface Contract.
- End with a single clean commit referencing this plan file and your phase number.
- If you discover the Frozen Interface Contract needs to change, you MUST update this
  section and clearly flag the change in your final report, since Phase 3 depends on
  Phase 2's sampler signature and Phase 6 depends on all of it staying consistent.
- Do not push to any remote or merge into `master` without explicit human approval.
