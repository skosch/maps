# Live Texture Shading for Hillshading — Implementation Plan

## Background

This project ("Ascend" / Stylus Labs Maps, a fork of tangram-es) currently renders
hillshading in `assets/scenes/hillshade.yaml` using a per-fragment 2nd-order
finite-difference normal calculation over a 3x3 elevation texel patch
(`hillshade.yaml:117-165`), feeding a standard Lambertian light. This looks good but
doesn't reproduce Leland Brown's "texture shading" technique (fractional Laplacian /
scale-invariant high-pass filter — see
https://mountaincartography.icaci.org/activities/workshops/banff_canada/papers/brown.pdf
and the newer seminar deck "Texture Shading Seminar 2024.pdf"), which produces an
isotropic, scale-independent emphasis of ridge/canyon network structure.

Brown's own algorithm is a **global, whole-region, offline FFT/DCT-domain operation**
— explicitly not solved for live tiled rendering even by its author (his own 2024
deck lists "Rendering by tiles" under "Future work — In progress"). We are doing
original engineering here, not porting a known recipe.

**Goal of this plan:** approximate texture shading as a **live, per-tile GPU shader
effect** that stays visually continuous when panning across tile boundaries, using:

1. A padded "mosaic" elevation texture per tile, stitched from the tile's own data
   plus its 8 neighbor tiles (already cached in `RasterSource`), so a tile's shader
   has real (not extrapolated) elevation data beyond its own edges.
2. Hardware-generated mipmaps (`generateMipmaps` + `textureLod`) over that mosaic to
   get genuinely low-pass-filtered (not aliased) multi-scale bands — i.e. a proper
   Laplacian/octave pyramid, not naive strided point-sampling.
3. A geometric per-octave band weight `2^(-k*alpha)` that is the mathematically
   correct discretization of Brown's power-law filter `|ν|^alpha` across octave
   bands — not an arbitrary tuning knob, `alpha` is the same exponent Brown exposes.
4. Brown's own closed-form contrast curve `σ(z) = z / (2*sqrt(1+z^2)) + 0.5`.
5. Blending with the existing Lambertian hillshade via the existing
   `HILLSHADE_BLEND_OVER` mechanism, not replacing it.

This is scoped specifically to make **"Ascend OSM Bike & Hike"**
(`stylus-bike-hike` in `assets/mapsources.default.yaml:49-54`, which composes
`stylus-osm.yaml` + the `hillshade` overlay) look good in both 2D and 3D. Fonts and
new/alternative shader techniques beyond this are explicitly out of scope for now.

## Non-goals (do not attempt these)

- Do not implement Brown's exact offline FFT/DCT algorithm. We are building a bounded,
  real-time *approximation*.
- Do not attempt a shared cross-tile mosaic texture / avoid the ~9x memory overhead
  of per-tile-independent mosaic stitching. That's a known, deliberately deferred
  optimization — note it in code comments but don't build it now.
- Do not touch fonts or any other shader (this plan is scoped to hillshading only).
- Do not merge to `master` or push to any remote without explicit human approval.

## Frozen Interface Contract

**All phases must conform to this. If a phase discovers it needs to change, it MUST
update this section and flag the change prominently in its final report — other
phases depend on these numbers being stable.**

- Applies **only** to the elevation raster source (the one associated with
  `ElevationManager`, configured in `assets/scenes/elevation.yaml`), gated by a new
  source-level flag. Do not change behavior for other raster overlay styles
  (worldcover, strava heatmaps, contour tiles, etc.) — they must render exactly as
  before.
- Let `W` = the tile's own decoded texture width (assert `width() == height()`,
  do not hardcode 256 — read it from the actual `Texture`).
- **Mosaic buffer**: `3W x 3W`, raw bytes-per-pixel matching the source's existing
  encoding (terrarium RGB or float — copy raw bytes, do not decode/re-encode).
  Tile's own data occupies the **center third**: pixel range `[W, 2W) x [W, 2W)`.
  The 8 neighbor tiles (N, S, E, W, NE, NW, SE, SW, same zoom) occupy the
  surrounding 8 cells in the natural grid arrangement.
- **Missing neighbor handling**: the CPU-side stitcher must *always* produce a fully
  populated `3W x 3W` buffer. Where a neighbor tile isn't cached yet, synthesize its
  cell via mirror-extrapolation of the tile's own edge data (same math idea as the
  existing shader-side fallback in `hillshade.yaml:140-143`, just done once on the
  CPU at texture-build time instead of per-fragment). The shader must never need to
  know which cells are "real" vs. synthetic — that complexity stays entirely on the
  CPU side.
- **Texture options**: the resulting mosaic texture must be uploaded with
  `TextureOptions.generateMipmaps = true` and
  `minFilter = TextureMinFilter::LINEAR_MIPMAP_LINEAR` (magFilter stays `LINEAR`,
  wrap stays `CLAMP_TO_EDGE`). This must be a per-source override, not a global
  default — other raster sources must keep their current texture options.
- **Shader UV remapping**: tile-local `uv` in `[0,1]` maps into mosaic space via
  `muv = (uv + 1.0) / 3.0`.
- **New GLSL function** (`assets/scenes/elevation.yaml`):
  ```glsl
  float getElevationAtLod(vec2 uv, float lod) {
      vec2 muv = (uv + 1.0) / 3.0;
      vec4 h = textureLod(u_rasters[ELEVATION_INDEX], muv, lod);
      // ... same decode as existing getElevationAt ...
  }
  ```
  Guarded behind a new define `ELEVATION_MOSAIC` so existing `getElevationAt`
  behavior (and all other raster styles) is unaffected when it's not set.
- **Pyramid depth**: compile-time define `TEXTURE_SHADING_MAX_LEVELS`, default `4`
  (revised in Phase 4 — see "Phase 4 integration findings" below the Phase 4 section;
  the original default of `7`, and the guard `2^level <= W`, are **not safe**: a GPU
  mip level `k` box-filters roughly a `2^k x 2^k` footprint of the mosaic, so even at
  `k=7` (`2^7=128`, technically `<= W=257`) that footprint already extends into the
  (frequently mirror-extrapolated, not real) neighbor cells for most pixels in the
  tile, not just near its edges. Keep `2^level` well under `W/2`, not just under `W`.)
- **Band weight**: `pow(2.0, -float(k) * u_texture_shading_alpha)`.
- **Contrast curve**: `float az = u_texture_shading_contrast * z; float shade = az / (2.0*sqrt(1.0+az*az)) + 0.5;`
- **New uniforms** (`assets/scenes/hillshade.yaml`), all exposed via `gui_variables`
  following the existing pattern in that file / `slope-angle.yaml:6-11`:
  - `u_texture_shading_alpha` (range 0..2, default 0.75 — Brown's recommended sweet spot)
  - `u_texture_shading_contrast` (default ~1.0 in the original spec; Phase 4 found this
    saturates the contrast curve almost everywhere against real meter-scale elevation
    data rather than Phase 3's small-amplitude synthetic test PNG — current default on
    this branch is `0.05`, itself not fully validated, see Phase 4 findings; needs
    real tuning in Phase 5, ideally with the curve made resolution/zoom-independent
    rather than a bare unnormalized multiplier against raw meters)
  - `u_texture_shading_opacity` (range 0..1, default 0.5; 0 = pure existing Lambertian look)
- **Blend**: mix the contrast-stretched `shade` value into `base_color`/`contour_color`
  using the existing `HILLSHADE_BLEND_OVER` pattern, mirroring how
  `slope-angle.yaml:68-76` mixes its `tint`. **Phase 4 correction**: mirroring that
  pattern means the tint's *alpha* must itself be spatially varying (as
  `slope-angle.yaml`'s `tint.a` is, from its gradient-texture lookup, ~0 on flat
  ground) — not the constant `u_texture_shading_opacity`. Use
  `ts_alpha = abs(ts_shade - 0.5) * 2.0 * u_texture_shading_opacity` as the alpha
  passed into the `mix(...)` calls, not `u_texture_shading_opacity` directly (the
  original Phase 3 code used the constant directly, which was a real bug — see
  Phase 4 findings above).

### Addendum from Phase 2 (implemented — read before starting Phase 1/3 work)

Phase 2 (CPU-side mosaic stitching) is implemented in `tangram-es/core/src/data/rasterSource.h/.cpp`.
The following clarifies/confirms details the contract above left ambiguous; nothing here
contradicts the contract, but flagging since Phase 1 and Phase 3 should rely on these being true:

- **Gating flag name**: `RasterSource::m_buildElevationMosaic` (public bool, default `false`,
  next to `m_keepTextureData`). Set to `true` **only** in `scene.cpp`, only inside the
  `if (m_elevationManager)` branch (i.e. only when 3D terrain / hillshade's elevation source is
  actually active) — this is a strict subset of the existing `m_keepTextureData` condition, so
  `m_buildElevationMosaic == true` always implies `m_keepTextureData == true` (the stitcher
  needs live CPU-side buffers for both the tile and its cached neighbors; without
  `m_keepTextureData`, `Texture::bufferData()` is freed right after GL upload and the stitcher
  has nothing to copy from).
- **Where the mosaic is built**: inside `RasterTileTask::addRaster()` (called from `complete()`
  / `complete(TileTask&)`, i.e. always on the **main thread**, both for a task's own primary
  completion and for zoom-adjusted raster subtasks attached to vector tiles — this is the actual
  runtime path hillshade/terrain-3d use, since the elevation raster is attached to vector tiles
  as a `RasterTileTask` subtask, not requested as its own top-level `TileSet`). This matters
  because `RasterSource::getTexture()` (used to look up neighbor textures) reads
  `RasterSource::m_textures`, which is documented as **not thread-safe** — safe here only
  because `addRaster()` never runs on a tile-worker thread.
- **The mosaic texture is never stored in `RasterSource::m_textures`.** That cache continues to
  hold only the plain per-tile `W x W` texture (used both as *other* tiles' neighbor data via
  `getTexture()`, and by `ElevationManager::getElevation()`/`getMinMaxElev()` for CPU height
  queries, which are completely unaffected by this change). The mosaic is a fresh `Texture`
  built on every `addRaster()` call and attached only to that call's `Tile::rasters()` entry —
  consistent with the plan's accepted non-goal of not sharing/caching mosaics across tiles.
- **Mirror-extrapolation is a pure byte/row-index flip, not the shader's `2*edge - inner`
  arithmetic formula.** A missing neighbor cell is filled by flipping the tile's own `W x W`
  buffer vertically (N/S), horizontally (E/W), or both (diagonals) — implemented in
  `fillMirroredNeighborCell()` in `rasterSource.cpp`. This was a deliberate deviation from a
  literal reading of "same math idea as `hillshade.yaml:140-143`": that shader formula decodes
  and linearly extrapolates pixel *values*, which requires knowing the pixel encoding (float vs.
  terrarium-RGB) — exactly what the contract's "copy raw bytes, do not decode/re-encode"
  requirement rules out doing generically. A flip is index-only (no decode), satisfies "not
  garbage/zero", and is only ever consumed by the coarsest mip levels per the contract's own
  tolerance for inexactness here.
- **Real-world tile size confirmed**: the actual `elevation.yaml` source
  (`elevation3d.arcgis.com`, LERC-encoded, `PixelFormat::FLOAT`) serves **257x257** tiles, not
  256x256 (verified by decoding real downloaded tiles with `Texture::loadImageFromMemory()` and
  running `stitchElevationMosaic()` on them: mosaic came out to exactly 771x771 = 3x257). This
  is exactly why the contract already says "do not hardcode 256" — confirmed necessary, not just
  cautious. `TEXTURE_SHADING_MAX_LEVELS` default `7` is still valid (`2^7=128 <= 257`).
- **NPOT/mipmap caveat (informational, not a contract change)**: `Texture::resize()` disables
  `generateMipmaps` if `Hardware::supportsTextureNPOT` is false and the size isn't a power of
  two (`gl/texture.cpp`) — our mosaic (`3W`, e.g. 771 or 768) is always NPOT. In production this
  is a non-issue: `Hardware::supportsTextureNPOT` is set from the real GL context in
  `Map::setupGL()` and is `true` for any GL3+/GLES3 context (this codebase's baseline, confirmed
  `#version 300 es` in `shaderSource.cpp`). It only matters for anything that constructs
  `Texture`s before `Hardware::loadCapabilities()` has run (not the case for tile textures in
  the normal app flow) or in tests without a GL context (see
  `tests/unit/rasterMosaicTests.cpp`, which sets `Hardware::supportsTextureNPOT = true` up front
  to simulate this).

---

## Phase 1 — Neighbor tile prefetching

**Objective:** whenever an elevation tile is requested for the visible viewport, also
proactively enqueue (low priority, background) fetches for its 8 neighbor tiles, so
that by the time Phase 2's stitcher runs, real neighbor data is available far more
often than not. Elevation tiles are cached for 2 years
(`assets/scenes/elevation.yaml:9-11`), so steady-state marginal cost is bounded to
roughly one extra ring around the viewport edge.

**Files to investigate** (not fully read yet — explore these first):
- `tangram-es/core/src/tile/tileManager.cpp` / `.h` — `TileManager::updateTileSet`
  (~line 296), `TileManager::enqueueTask` (~line 94), `TileSet::visibleTiles`
  (~line 81), `m_workers.enqueue` (~line 126) — check if the task queue supports a
  priority parameter.
- `tangram-es/core/src/scene/scene.cpp` (~lines 279-291) — where the elevation
  `RasterSource`/`ElevationManager` gets constructed and `m_keepTextureData = true`
  is set; this is the natural place to also set a new prefetch-enable flag.
- `tangram-es/core/src/data/rasterSource.cpp` — `RasterSource::createTask` (~line 185).
- `tangram-es/core/src/util/elevationManager.h/.cpp`.
- Check `TileID` (find its header, likely `tangram-es/core/src/util/tileID.h` or
  similar) for existing wraparound-aware helpers (antimeridian x-wrap at `2^z`,
  y-clamping at poles) before writing new neighbor-ID math from scratch.

**Approach:**
1. Add a `bool m_prefetchNeighbors = false;` flag to `RasterSource` (or reuse/extend
   whatever flag Phase 2 needs — coordinate via this plan file, not by guessing).
   Set it `true` only for the elevation source, near `scene.cpp:291`.
2. In `TileManager::updateTileSet` (or a small new helper it calls), for each
   `TileID` in `visibleTiles` belonging to a `TileSet` whose source has
   `m_prefetchNeighbors == true`, compute its 8 neighbor `TileID`s and enqueue them
   through the existing task-creation path, at lower priority if supported.
3. Neighbor-only tiles must NOT trigger label/mesh/geometry building — verify
   `RasterSource::generateGeometry(false)` already holds for the elevation source
   (it should, since it's raster-only) rather than assuming.
4. Gate this so only the elevation `TileSet` is affected; other sources' tile sets
   must not grow.

**Acceptance criteria (must be verifiable without a display):**
- Project builds (desktop target) with no new warnings in changed files.
- Add temporary `LOGD` logging when a neighbor-prefetch task is enqueued and when its
  texture finishes loading/caching.
- Run the desktop app against a 3D-terrain-enabled style; pan the camera (check
  `tangram-es/core/tests` and `app/` for any existing scripted-camera/dev-input
  mechanism to automate this; if none exists, manual keyboard/mouse pan is
  acceptable for this smoke test). Confirm via logs that neighbor tiles for
  currently-visible elevation tiles get requested and end up in
  `RasterSource::m_textures` (i.e. `getTexture()` returns non-null) even though they
  were never themselves visible.
- Confirm non-elevation raster sources are unaffected (log/assert their tile-set
  sizes are unchanged from current behavior).
- Single commit on branch `texture-shading-phase1-prefetch`, message referencing
  `docs/texture-shading-plan.md` Phase 1. Do not merge to `master`.

---

## Phase 2 — CPU-side mosaic stitching

**Objective:** when an elevation tile's texture is built, produce a `3W x 3W`
"mosaic" texture per the Frozen Interface Contract above, uploaded with hardware
mipmaps enabled, replacing the plain per-tile texture used for elevation sampling.

**Files:**
- `tangram-es/core/src/data/rasterSource.h/.cpp` — `RasterTileTask::process()`
  (~lines 34-62), `RasterSource::createTexture` (~line 129),
  `RasterSource::getTexture` (~line 220), `RasterSource::cacheTexture` (~line 193).
- `tangram-es/core/src/gl/texture.h/.cpp` — `Texture::bufferData()`,
  `setPixelData`, `resize`, `TextureOptions.generateMipmaps` (already wired to
  `GL::generateMipmap` in `texture.cpp:128-129` — confirm this before assuming).
- `tangram-es/core/src/scene/scene.cpp` (~line 291) — confirm `m_keepTextureData`
  timing: it must be set before any elevation tiles are requested, and it must
  apply to *all* textures the elevation `RasterSource` creates (including
  neighbors), not just the "current" tile.

**Design (must match the Frozen Interface Contract exactly — read it first):**
1. Gate this entire mosaic-build path behind the elevation-only flag from Phase 1
   (or a dedicated new flag, e.g. `RasterSource::m_buildElevationMosaic` — if you
   introduce a new flag, update the Frozen Interface Contract section of this file
   to record it precisely, since Phase 1 and Phase 3 need to agree on the name).
2. `W = texture->width()`; assert square.
3. Allocate `3W x 3W` raw buffer, same bytes-per-pixel as source encoding (do not
   decode). Copy the tile's own buffer into the center `[W,2W) x [W,2W)` region.
4. For each of the 8 neighbor `TileID`s: look up via `getTexture(neighborId)`. If
   found and its `bufferData()` is non-null, copy its full `W x W` buffer into the
   corresponding mosaic cell. If not found, synthesize that cell via mirror
   extrapolation of the tile's own edge (write a small reusable helper function for
   this, e.g. `fillMirroredNeighbor(...)`). For a missing diagonal neighbor when
   both adjacent edge neighbors are present, mirror from the nearest available edge
   data — doesn't need to be exact, it only feeds the coarsest pyramid levels.
5. Upload as a new `Texture` with `generateMipmaps = true`,
   `minFilter = LINEAR_MIPMAP_LINEAR`, replacing the elevation raster entry in
   `Tile::rasters()`.
6. Keep `wrapS`/`wrapT` at `CLAMP_TO_EDGE` — the mosaic already contains real
   neighbor data, it must not rely on GL texture repeat/wrap.
7. Write the stitcher generically over raw bytes-per-pixel (don't assume terrarium
   RGB vs. float encoding — check `assets/scenes/elevation.yaml`'s
   `ELEVATION_FLOAT_TEX` define and make sure both paths work, since different
   elevation sources may use either).
8. Document the exact mosaic cell layout in a code comment at the stitching
   function.

**Acceptance criteria:**
- Add a unit test (check `tangram-es/tests/unit/tileManagerTests.cpp` for the test
  framework/conventions already in use) that constructs a fake tile + 8 fake
  neighbor textures with known distinct constant pixel values, runs the stitcher,
  and asserts the resulting `3W x 3W` buffer has the correct values in the correct
  cells — including at least one test case with 1-2 neighbors deliberately missing,
  asserting the fallback produces mirrored (not garbage/zero) values.
- Project builds; all existing unit tests still pass.
- Runtime log check: for a real loaded elevation tile, log and confirm the mosaic
  buffer's dimensions are exactly `3W x 3W`.
- Single commit on branch `texture-shading-phase2-mosaic`, message referencing
  `docs/texture-shading-plan.md` Phase 2. Do not merge to `master`.

---

## Phase 3 — Shader-side multi-scale texture shading

**Objective:** implement the Laplacian-pyramid-via-mipmap approximation inside
`hillshade.yaml`/`elevation.yaml`, per the Frozen Interface Contract, blended with
the existing Lambertian hillshade.

**Files:** `assets/scenes/elevation.yaml`, `assets/scenes/hillshade.yaml`.

**This phase has no compile-time dependency on Phase 2's C++ code** — it only needs
the Frozen Interface Contract to hold (mosaic = `3W x 3W`, tile's own data centered
in the middle third, real GPU mipmaps enabled). Develop and test it against a
**hand-built synthetic elevation PNG**: write a small script (Python + Pillow, or
ImageMagick) producing a `768x768` (`3x256`) terrarium-encoded PNG with a known
pattern (e.g. a ramp plus a few sharp ridges), and temporarily point
`assets/scenes/elevation.yaml`'s source `url:` at this local file (there's precedent
— `elevation.yaml:14` shows the source is a simple swappable URL) so this phase can
be built and verified independently of Phase 2 being merged yet.

**Design (see Frozen Interface Contract for exact formulas — do not deviate without
updating that section):**
1. In `elevation.yaml`, add `getElevationAtLod(vec2 uv, float lod)` per the contract,
   guarded by `#ifdef ELEVATION_MOSAIC`.
2. In `hillshade.yaml`, add a function (e.g. `float textureShading(vec2 uv)`) that:
   - Loops `k` from `0` to `TEXTURE_SHADING_MAX_LEVELS - 1`.
   - Samples `getElevationAtLod(uv, float(k))` and `getElevationAtLod(uv, float(k+1))`,
     differences them for band `k`.
   - Weights band `k` by `pow(2.0, -float(k) * u_texture_shading_alpha)`.
   - Sums into `z`, then applies the contrast curve from the contract to get `shade`.
3. Blend `shade` into `base_color`/`contour_color` via the existing
   `HILLSHADE_BLEND_OVER` pattern (mirror `slope-angle.yaml:68-76`), scaled by
   `u_texture_shading_opacity`.
4. Add `gui_variables` for `u_texture_shading_alpha`, `u_texture_shading_contrast`,
   `u_texture_shading_opacity` following the exact existing pattern in
   `hillshade.yaml` / `slope-angle.yaml:6-11`.
5. Backward compatibility: if `ELEVATION_MOSAIC` is undefined, the texture-shading
   block must compile out entirely (`#ifdef`) rather than crash or silently degrade —
   document which you chose.

**Acceptance criteria:**
- Scene YAML parses without error against the synthetic elevation source (check
  `tangram-es/tests` for any existing YAML/scene-loading test harness to reuse).
- Build the app, load a style with `hillshade` + this new block pointed at the
  synthetic source. Investigate `glfwmain.cpp`/`app/src/mapsapp.cpp` for an existing
  screenshot/frame-dump debug command (grep for "screenshot"/"capture"/"png"); if
  one exists, use it to capture a PNG. If none exists, add a minimal one (e.g. a
  debug keybinding or CLI flag that dumps the current framebuffer to a PNG at
  startup) — this is a reusable tool worth building regardless.
- Confirm programmatically (e.g. compare pixel variance/contrast of the captured
  image with `u_texture_shading_opacity=0` vs. `0.5`) that the block has a visible,
  non-degenerate effect.
- No shader compile errors/warnings in app log output. Check `shaderSource.cpp` for
  which GL/GLSL variants are actually built (`#version 300 es` was confirmed in use)
  and make sure `textureLod` compiles cleanly there.
- Single commit on branch `texture-shading-phase3-shader`, message referencing
  `docs/texture-shading-plan.md` Phase 3. Do not merge to `master`.

---

## Phase 4 — Integration, build, smoke test

**Objective:** merge phases 1-3, verify they work together against real (not
synthetic) elevation tiles, and smoke-test.

**Steps:**
1. Merge `texture-shading-phase1-prefetch`, `texture-shading-phase2-mosaic`,
   `texture-shading-phase3-shader` (in that order). Expected conflicts are minimal
   given the file separation (Phase 1: `tileManager.cpp`/`scene.cpp`; Phase 2:
   `rasterSource.cpp`/`texture.cpp`; Phase 3: YAML scenes) — resolve any that arise,
   and if resolving requires deviating from the Frozen Interface Contract, update
   this file to reflect the final, true state.
2. Enable `ELEVATION_MOSAIC` for the real elevation style (wire up whatever
   conditional Phase 3 used, pointed at the real elevation source, not the synthetic
   test one).
3. Locate and run the desktop build (check `CMakeLists.txt`/`Makefile`/`make/` at
   repo root for the exact target/command).
4. Run the app with the `stylus-bike-hike` style
   (`assets/mapsources.default.yaml:49-54`) with 3D terrain and texture shading
   enabled, over real hilly terrain. Confirm via logs: no GL errors (check `gl.h`/
   `renderState.cpp` for an existing error-checking utility), no crashes,
   neighbor-prefetch and mosaic-stitch log lines firing as expected.
5. If a screenshot tool was built in Phase 3, use it to capture before/after frames
   panning across a tile boundary, for human review — do not attempt to judge visual
   quality yourself; that's Phase 5.
6. Single commit (or small commit series) on branch `texture-shading-phase4-integration`.
   **Do not merge to `master` or push to any remote — stop and hand back for human
   review**, per this project's standing git safety rules.

### Phase 4 integration findings (real, non-tuning bugs found running all 3 phases together)

Merging phases 1-3 and running them together against **real** elevation tiles (never
done before — each prior phase tested in isolation) surfaced three real bugs, not
just visual-tuning issues. Two are fixed on this branch; the third is NOT fixed and
is why `global.elevation_mosaic` is left `false` (see `assets/scenes/elevation.yaml`)
— i.e. **texture shading is fully implemented and wired end-to-end but disabled by
default**, pending follow-up work below.

1. **Fixed — blend-alpha bug (`assets/scenes/hillshade.yaml`).** The texture-shading
   blend used `vec4(ts_tint.rgb, u_texture_shading_opacity)` as the tint passed into
   the `mix(base_color, tint, 1.0/(1.0+base_color.a))` pattern copied from
   `slope-angle.yaml`. That pattern only works because slope-angle's `tint.a` is
   *itself spatially varying* (near 0 on flat ground, from its gradient-texture
   lookup), so flat areas contribute ~nothing to the final composite. Texture
   shading instead passed a flat *constant* alpha (`u_texture_shading_opacity`)
   everywhere, so perfectly flat/neutral terrain (`ts_shade == 0.5`) got the exact
   same blend strength as a real ridge — washing the whole map in flat gray rather
   than only tinting real ridge/canyon structure. Fixed by scaling alpha by the
   deviation of `ts_shade` from its neutral midpoint: `ts_alpha = abs(ts_shade -
   0.5) * 2.0 * u_texture_shading_opacity`.
2. **Fixed (partial mitigation) — pyramid depth vs. real tile size.** The Frozen
   Interface Contract's guard for `TEXTURE_SHADING_MAX_LEVELS` ("must not exceed
   what a single neighbor ring can support, i.e. `2^level <= W`") is not a safe
   bound in practice. A GPU mip level `k` box-filters roughly a `2^k x 2^k` texel
   footprint of the *mosaic*; at the contract's default `k=7` (`2^7=128`, barely
   under the real `W=257`), that footprint already extends into the neighbor cells
   for most pixels in the tile, not just near its edges. Neighbor cells are
   frequently mirror-extrapolated (Phase 2) rather than real when a neighbor hasn't
   loaded yet, and since texture shading is a high-pass filter, it makes that
   synthetic mirrored/point-reflected structure blatantly visible as a repeating,
   "kaleidoscope" tile pattern — something the original gentle 3x3-texel Lambertian
   normal calc never exposed. Reduced default to `TEXTURE_SHADING_MAX_LEVELS: 4` so
   the footprint stays mostly inside real tile data for most pixels. **This helped
   but did not fully resolve the pattern** — see finding 3.
3. **NOT fixed — neighbor-prefetch/mosaic-build congestion at realistic zoom levels.**
   At any view showing more than a handful of tiles (e.g. a city-wide zoom, or even
   a normal ~1km-wide zoomed-in view — both tested), Phase 1's prefetch enqueues 8
   neighbor fetches *per visible tile*, and Phase 2's `RasterTileTask::addRaster()`
   unconditionally built a full `3W x 3W` mosaic (with its own 8 `getTexture()`
   lookups) for **every** one of those prefetch-only tiles too, even though
   prefetch-only tiles are never rendered. With dozens of visible tiles this means
   hundreds of wasted mosaic builds competing for the same limited tile-worker pool
   as real (non-elevation, e.g. OSM vector) tile loads. Observed effects: the app's
   CPU usage saturated (e.g. 3.5 CPU-minutes consumed in under a minute of
   wall-clock), the OSM base vector layer did not render at all even after 60+
   seconds, and most elevation neighbors still hadn't resolved to real data (heavy
   reliance on mirror-extrapolation) well after startup. Applied one mitigation on
   this branch — skip `buildElevationMosaic()` when `task->isProxy()` is true
   (covers both prefetch-only tiles and genuine lower-zoom placeholder/proxy tiles;
   see `RasterTileTask::addRaster()` in `rasterSource.cpp`) — which measurably
   reduced CPU load, but **the OSM base layer still failed to render and the
   kaleidoscope pattern still persisted** in testing after this fix. The remaining
   cause is not fully isolated; plausible contributors that need dedicated
   follow-up investigation:
   - Even only for genuinely *visible* tiles (not skipped by the `isProxy()` fix),
     a wide view can have dozens of them, each doing a full mosaic stitch — may
     still be enough to starve the worker pool on its own.
   - The plan's own **Non-goal** ("do not attempt a shared cross-tile mosaic
     texture... deliberately deferred optimization") may be a more fundamental
     problem than anticipated: each tile independently mip-maps its *own copy* of
     neighboring pixel data, so even with 100% real (non-mirrored) neighbor data,
     independently-built mosaics for adjacent tiles are not guaranteed to produce
     bit-identical high-pass results at the shared boundary. A high-pass filter
     (texture shading) amplifies exactly this kind of tiny cross-tile
     inconsistency far more visibly than the original Lambertian shading ever did.
     Confirming whether this is the dominant cause (as opposed to simply "not
     enough neighbors loaded yet") needs a controlled test with a fully warm,
     complete elevation cache for the entire visible area before judging the
     steady-state visual result — not yet done.
   - Should also double check whether repeated killing/relaunching of the app
     during this investigation (many times, to change config) left the
     `TileWorker` thread pool or `m_loadTasks` queue in some unusual state; a
     single long-running session was not tested end-to-end after the `isProxy()`
     fix.

**Recommendation:** do not flip `global.elevation_mosaic` to `true` for production
until finding 3 is root-caused and fixed. The plumbing (Phase 1 prefetch, Phase 2
stitching, Phase 3 shader) all work correctly in isolation and the mechanics were
verified end-to-end (real 257x257 tiles → 771x771 mosaics, logged, no GL errors, no
crashes) — this is a real-data-only visual/performance problem, not a wiring bug.

---

## Phase 5 — Visual tuning (human-in-the-loop, not an agent task)

Once Phase 4 is reviewed and running, check by hand:
- Default `alpha`/`contrast`/`opacity` look good across a few real terrain samples
  (mountains, canyons, gentle hills).
- Tile-boundary continuity when both sides are loaded.
- Graceful (not obviously broken) appearance when a neighbor hasn't loaded yet.
- Frame-time/performance impact of the new per-fragment loop, especially on
  lower-end/mobile devices.
- Whether a single ring of neighbors is visually sufficient, or a second ring is
  worth the added memory/complexity later.

---

## Execution notes for agents

- Each phase agent should work in an isolated git worktree, branching from `master`,
  so phases 1-3 can proceed in parallel without clobbering each other.
- Read this entire file first — it is your sole shared context. Do not assume access
  to any prior conversation history.
- End with a single clean commit referencing this plan file and your phase number.
- If you discover the Frozen Interface Contract needs to change based on what you
  find in the real code, you MUST update that section of this file and clearly flag
  the change in your final report, since other phases depend on it staying stable.
- Do not push to any remote or merge into `master` without explicit human approval.

### Resolution addendum (post-Phase 4 rework, 2026-07-05)

The Phase 4 findings above are superseded as follows:

- **Kaleidoscope artifact — root cause found and fixed.** It was never mip-footprint
  bleed: `getElevationAt()` (used by the normal/contour code and 3D-terrain vertex
  displacement) was sampling the mosaic *without* the center-cell UV remap that only
  `getElevationAtLod()` had, so normals were computed over the whole 3×3 mosaic.
  Fixed in `elevation.yaml` (shared `sampleElevationTex()` remap, explicit LOD 0).
- **Mirrored fallback is now transient.** `RasterSource::patchNeighborMosaics()`
  copies real tile data over mirror-extrapolated mosaic cells in place as neighbor
  tiles (including Phase 1 prefetches) arrive, and mosaics are shared/deduplicated
  per TileID via a registry (`m_mosaics`) — also removing the per-Tile restitching
  that drove the congestion finding.
- **Two real stitching bugs found by visual testing:** (1) mosaic cell row placement
  had N/S inverted (buffer rows run south→north, opposite to tile y — bright/dark
  bands along horizontal tile boundaries); (2) node-registered 257×257 tiles share
  their outermost row/col with neighbors, and naive tiling duplicated that line
  (thin seams on all boundaries). Both fixed in `rasterSource.cpp`, covered by a
  global-seamlessness unit test.
- **Zoom continuity:** the shader pyramid is now anchored to continuous view zoom via
  a fractional base LOD (`b = viewZoom − tileZoom`), with matching normalization, so
  shading no longer jumps at tile-zoom transitions.
- **Texture shading is now a user toggle** ("Texture shading" checkbox next to
  "3D terrain") driving `global.elevation_mosaic`; the Lambertian hillshade stays
  active underneath, and all texture-shading parameters (alpha, contrast, opacity,
  levels, scale shift, blend mode) are GUI sliders in `hillshade.yaml` for Phase 5
  tuning.

### Land cover in 2D/3D + tile-seam fixes (2026-07-06)

Follow-up work on this branch, building on the texture-shading integration:

- **Land cover with 3D terrain.** `stylus-bike-hike` (and every vector base + hillshade
  combination) now shows land cover / landuse polygons in 3D as well as 2D:
  - The standalone `hillshade` style renders the same translucent blend-over shading in 3D
    that it always did in 2D (the opaque `vec4(0.88,...)` 3D branch is gone); the opaque
    terrain surface behind the polygons comes from a new `terrain-ground` raster style /
    `layers.earth` draw rule at order 50 (below all polygons; in 2D it degenerates to a flat
    quad matching the background color). Land cover polygons (orders 510-590) draw between
    the ground and the shading, identically composited in 2D and 3D.
  - The old `terrain_3d: updates: global.show_land_polygons: false` default is removed from
    `config.default.yaml`, and `MapsApp::loadConfig()` drops the stale key from existing
    user configs on version upgrade.
  - **Polygon grid tessellation** (`terrain_grid` style parameter, set on `unlit-polygons`;
    `PolygonBuilder::gridRes`, active only when the scene has an `ElevationManager`): the
    whole polygon is earcut as usual, then each output triangle is clipped along the tile's
    64x64 grid lines (matching the `RasterStyle` terrain mesh resolution; clipping triangles
    keeps every piece convex, which Sutherland-Hodgman handles exactly - clipping the
    non-convex *rings* per cell instead produced degenerate bridge geometry and mixed-winding
    earcut output that face culling then swallowed). Cells fully inside a triangle are
    emitted with the terrain mesh's own diagonal orientation, so the draped surface matches
    the terrain surface exactly there; per-vertex elevation then keeps big landuse polygons
    on the terrain at any distance instead of letting ridges poke through (or the polygon
    paint over ridges in front of it). Unit tests: `tests/unit/buildersTests.cpp`.
- **View-angle fade.** With 3D terrain, hillshading and texture shading fade out linearly
  with camera tilt (full strength at zenith, gone at >= 45 deg, `shade_fade` in
  hillshade.yaml) - the 3D geometry itself conveys relief once the camera is tilted;
  contours and hypsometric tint are unaffected.
- **Tile-seam root cause (the "still some edge artefacts" report).** Two fixes:
  1. The dominant, deterministic seam on every boundary: `RasterSource::m_textures` holds
     only weak refs, and Tiles reference the *mosaic* instead of the original per-tile
     texture, so the original expired as soon as its mosaic was stitched; any neighbor
     stitched later couldn't find the (still rendered!) tile's data and permanently used
     mirror-extrapolation for that cell (`patchNeighborMosaics()` only fires on newly cached
     tiles). Mirrored-vs-real content in adjacent mosaics diverges within a few texels of
     the shared edge, so even the finest bands seamed. Fixed by keeping the original texture
     alive via `mosaic->userData`. Verified byte-identical shared cells (instrumented
     comparison: max diff went from ~1200 m to 0) and gradient-spike-free boundaries.
  2. Defense in depth for the deepest bands: `getElevationAtLod()` caps the mip LOD so the
     bilinear footprint (~1.5 * 2^lod texels) stays within +/- one tile of the pixel - past
     that, adjacent tiles' independently-built mosaics integrate over different data (they
     share only 2 of 3 mosaic cells) and cannot agree; capped bands difference to zero and
     fade out instead of seaming. This also excludes the NPOT 3x3 -> 1x1 mip tail.
- **Default `u_texture_shading_alpha` is now 0.6** (was 2.0): broad-landform emphasis reads
  far better and no longer washes out land cover colors on steep terrain, and is viable now
  that the low-alpha (deep-band) seams above are fixed.
