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
