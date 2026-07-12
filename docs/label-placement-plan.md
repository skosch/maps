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

Phases 2 and 3 both need to evaluate "how strongly does texture shading emphasize this
point" from CPU code running in tile-build worker threads (`TextStyleBuilder` /
`PointStyleBuilder`), not the GPU fragment shader. This must be a literal CPU port of the
already-frozen shader formula in `docs/texture-shading-plan.md` — not a new heuristic
(e.g. not a simple local min/max relief calculation) — so that label-placement decisions
stay visually consistent with what the hillshade layer actually renders.

- New file: `tangram-es/core/src/util/textureShading.h/.cpp` (or nearest existing
  location for small shared elevation utilities — check `elevationManager.h/.cpp` first
  in case it's a more natural home; if so, update this section to say which).
- Signature (adjust types to match what's actually available, but preserve this shape):
  ```cpp
  // Returns the same [0,1]-ish contrast-curved "shade" value the GPU shader would
  // compute at this world position, using CPU-side buffer reads instead of textureLod.
  // Returns false via `ok` if the elevation mosaic isn't resident/available here.
  float sampleTextureShading(const RasterSource& elevationSource,
                              ProjectedMeters worldPos, bool& ok);
  ```
- **Must reuse the exact formulas already pinned in `docs/texture-shading-plan.md`**:
  band weight `pow(2.0, -k * alpha)`, contrast curve
  `az = contrast * z; shade = az / (2*sqrt(1+az*az)) + 0.5`, same `alpha`/`contrast`
  defaults as the shader uniforms (`u_texture_shading_alpha`, `u_texture_shading_contrast`
  — read the current values from the scene's `hillshade` style config rather than
  hardcoding, if that's accessible from a style-builder context; otherwise hardcode the
  same defaults and flag it).
- Octave levels are produced by **software box-filter downsampling** of the raw CPU
  pixel buffer (`Texture::bufferData()`) in place of hardware mip levels — average
  2×2 blocks repeatedly, matching what `GL_LINEAR_MIPMAP_LINEAR` would produce closely
  enough for this purpose. Pyramid depth: same `TEXTURE_SHADING_MAX_LEVELS` constant,
  keep `2^level` well under `W/2` per the corrected note already in
  `docs/texture-shading-plan.md`.
- Operates on the same `3W×3W` mosaic buffer layout Phase 2 of the texture-shading plan
  established (tile's own data in the center third, real or mirror-extrapolated
  neighbors around it) — do not re-derive a different buffer layout.
- **Threading**: this is the single biggest open risk in this plan. `TextStyleBuilder`/
  `PointStyleBuilder::addFeature` run on background tile-worker threads
  (`tileWorker.cpp`); confirm whether `RasterSource`'s cached `Texture::bufferData()` for
  the elevation source can be safely read concurrently from such a thread (look at how
  `RasterSource::getTexture`/`m_textures` synchronization already works, and whether
  anything mutates that map off the main thread). If genuinely unsafe within reasonable
  effort, the documented fallback is: compute this once per visible peak **on the main
  thread**, piggybacking on the existing per-frame label-update pass in
  `LabelManager`/`Scene::render`, and cache the result on the feature/label so it isn't
  recomputed every frame. Document whichever path was taken here, since Phase 3 depends
  on it.
- **Availability fallback**: if the elevation source/mosaic isn't loaded for a given
  peak's location (flat/non-terrain map style, or tile not yet cached), `ok = false` and
  callers fall back to today's behavior (Phase 2: pure elevation weighting; Phase 3: skip
  the texture-shading term, use only the vector-feature-proximity term).

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

**Objective:** rank peak label priority by real topographic salience instead of raw
elevation alone.

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
