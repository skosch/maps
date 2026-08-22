# Rock Texture v2 — Implementation Plan

Successor to the original procedural rock-texture shader (`0549c64`, "Add procedural
rock-texture shader for bare-rock terrain"). That version produced a flat single hex color
(`global.color.rock`) multiplied by a scalar derived from one ridged-fbm noise layer — no
branching erosion structure, no material/color variation, no strata/bedding, and no actual
surface-normal perturbation feeding real lighting (a pure albedo multiply can't reproduce
self-shadowed crevices and lit facets that respond to light direction). This plan replaces
it with a budget-conscious, fully procedural rewrite that keeps the same live-tuning
workflow (GUI sliders via `global.gui_variables`).

Committed directly to master (`[[work-on-master-directly]]`), no worktree/branch.

## Sources consulted

- shadertoy 7ljcRW ("Terrain Erosion Noise", Fewes/clayjohn) and the runevision "Fast and
  Gorgeous Erosion Filter" writeup it's based on: branching gully/ridge generation from
  gradient-aligned stripe fields blended across jittered cell pivots, without any water
  simulation — every point evaluated independently, GPU-friendly.
- planetside.co.uk's Terragen terrain-technique series (image-map / simple-shape / detail-
  warp-erode): the general shape → warp → erode → strata/outcrops production workflow, and
  the idea of selecting material by erosion mask (ridges vs. gullies) rather than a single
  flat color.
- DSRFlow (DEM super-resolution via normalizing flow, `10.1038/s41598-025-94274-w`): not
  directly portable (offline ML, not real-time), but its multi-scale frequency decomposition
  and slope/Terrain-Ruggedness-Index-based realism validation informed the layering
  discipline here (macro DEM shape → meso erosion/strata → micro grain, each with a distinct
  job) more than any single formula.
- Klaus & Hagg 2024, "Development of a New Approach for a Digital, Cartographic Rock and
  Relief Representation" (`10.1007/s42489-024-00162-0`) — Swiss analytical-cartography
  lineage (Hurni/Jenny), the same tradition already cited as inspiration for this project's
  screen-anchored "generalizes Imhof" sun design (`docs/terrain-lighting-v2-plan.md`).
  Reinforces that rock representation needs to work at any scale/rock-type without
  ground-truth geology data, validating the "one good generic material" scope decision.

## Key architectural finding: real lighting without touching hillshade's compositing

In 3D, `hillshade.yaml` renders a *separate* translucent blend-over pass on top of all
land-cover polygons, supplying macro directional shading from the DEM. `rock-texture` is
`lighting: false` and sits underneath that pass.

Flipping it to `lighting: true` and routing a perturbed normal through the engine's built-in
`calculateLighting()` (via `tangram-es/core/shaders/polygon.fs`'s `#pragma tangram: normal`
hook — real, available, demonstrated by the `normals` example style in
`tangram-es/res/scene.yaml`) was considered and rejected: `calculateLighting()` uses each
light's configured direction as-is, and `light2`'s default `[1,-1,-1]` is **world-anchored**,
not screen-anchored, unless explicitly transformed — the exact bug `hillshade.yaml` had to
fix in 3 revisions (see `docs/terrain-lighting-v2-plan.md`), which is why hillshade bypasses
`calculateLighting` and rolls its own screen-anchored dot product instead
(`hillshade.yaml`'s `s_world` computation, now `screenAnchoredSunDir()`). Using the built-in
path here would reintroduce that already-solved bug class and desync from hillshade's macro
shading under map rotation.

**Chosen approach**: `rock-texture` computes a real perturbed normal and does its own cheap
hand-rolled Lambertian dot-product with the *same* screen-anchored sun direction, inline in
its existing `color:` block — no `normal:` pragma, no `calculateLighting()`. This still
fully satisfies "normal noise before lighting is computed": a real geometric normal,
perturbed by procedural crack/facet detail, dotted against a real light direction, determines
the final shading. It's just a small dedicated term rather than the engine's generic
multi-light path, for the same reason hillshade itself avoids it.

To make the sun direction shareable, `screenAnchoredSunDir(azimuthDeg, elevDeg)` was
extracted (byte-identical formula) from `hillshade.yaml` into `elevation.yaml`'s
`get-elevation` style (`shaders.blocks.global`), which both `hillshade` and `rock-texture`
(via `unlit-polygons → terrain_3d_mixin=terrain-3d → mix:[get-elevation, fog]`) already
transitively pull GLSL helpers from (`getElevationAt` etc. worked this way before this
change too). It's a pure function of `(azimuth, elevation)` + `u_view` (unconditionally
available in every style) — callers own their own azimuth/elevation values, the function
just owns the formula. `hillshade.yaml` now calls it instead of inlining it.
`rock-texture` calls it with fixed constants (135°/35.264°, matching hillshade's own
defaults) rather than its own GUI-tunable uniforms — deliberately, so a live hillshade
sun-azimuth/elevation slider tweak can't silently desync the two shading layers; this is a
small micro-facet accent, not the macro knob Sebastian actually tunes.

**C++/submodule engine changes were considered, not needed.** Everything above (per-style
uniforms, `u_view`, raster-sampling helpers, shared GLSL via style `mix:` chains) is already
exposed at the YAML/GLSL scene level. One engine-adjacent alternative was named and deferred:
baking a small procedural rock normal-map texture once (offline or a one-time runtime
render-to-texture pass) and sampling it through the engine's existing triplanar material
system (`material.glsl`, `u_material_normal_texture`/`getTriPlanar`) — would get real
hardware mipmapping (a strictly better anti-aliasing story than the analytic `fwidth()`-based
fade below), at the cost of a new baked-asset/caching mechanism this project's procedural
texture/hillshade code deliberately doesn't have. Revisit only if the analytic aliasing
mitigation proves insufficient once Sebastian looks at it live.

## Implementation

All changes scene-only: `assets/scenes/{elevation,hillshade,stylus-osm}.yaml`.

1. **Structural noise** (`stylus-osm.yaml`, `rock-texture`'s `global` block —
   `rockCellStripe`/`rockErosionOctave`/`rockErosionFbm`): budget-limited adaptation of the
   shadertoy/runevision technique. Per octave, 2 jittered cell pivots (not N), each producing
   a sine-stripe field oriented via a mix of a per-cell random angle and the real local
   downhill direction (correlates synthetic cracks with actual DEM gullies); the two fields
   are *summed* (unaligned-sine-sum trick — automatically smaller amplitude at the seam, no
   explicit crossfade weight needed). A running fade mask keeps finer octaves from painting
   over established coarse ridge crests; a `fwidth()`-based per-octave amplitude fade
   self-attenuates frequencies that would alias under minification (this pattern has harder
   creases than the old smooth ridged-fbm, so it aliases more readily while panning). Returns
   both the structural signal and a 0-1 ridge-crest-closeness mask, reused by both the
   material blend and the normal perturbation below (not resampled).
2. **Strata/bedding** (`rockStrata`): one low-frequency directional banding term, axis
   randomized per ~200m spatial cell (geological, deliberately *not* slope-derived, unlike
   the erosion pattern), lightly warped by the existing noise so bands aren't dead-straight.
   `u_rock_strata_strength`/`u_rock_strata_scale`.
3. **Material/color blend**: two new palette entries, `global.color.rock_shadow` (damp/
   weathered gully rock) and `global.color.rock_lichen` (muted crust accent). Blends
   `global.color.rock` toward `rock_shadow` weighted by ridge-mask/curvature/strata; a small
   further mix toward `rock_lichen` masked to low-slope + concave microsites via its own slow
   noise tap (so lichen placement doesn't visibly track the crack pattern 1:1).
   `u_rock_strength` keeps its original meaning (0 = flat original color, 1 = full effect).
4. **Bump-perturbed lighting**: macro normal from the already-computed `gradTrue` (5-tap
   elevation stencil, reused — no new taps); micro perturbation via finite-difference of the
   structural noise (center + dx + dy) at a *fixed* reduced octave count (2, independent of
   the `u_rock_octaves` slider — bump detail beyond that doesn't register in lighting even
   where visible in albedo, the main cost-control lever). Lit via
   `screenAnchoredSunDir(135.0, 35.264)`. Critically, the visible term is the **delta**
   between `dot(perturbedNormal, sun)` and `dot(macroNormal, sun)`, not the absolute dot
   product — this keeps it a pure micro-facet accent instead of a second, redundant
   macro-slope relight (hillshade's blend-over already owns that). At
   `u_rock_normal_strength = 0`, `perturbedNormal == macroNormal` exactly, an honest no-op.

## Perf

`style-gpu:unlit-polygons` (which `rock-texture` mixes from) was already ~29ms/frame mean GPU
time, one of the two dominant GPU costs app-wide (`[[profiling-system-state]]`) before this
change. The new 2-cell structural noise is ~2x cheaper per-octave than the old 4-corner
bilinear value noise; the bump finite-difference evaluates it 3x but at a fixed low octave
count decoupled from the albedo slider — net expectation is roughly parity with the old
shader, not a multiplicative blowup, but this is an estimate. `style-gpu:unlit-polygons` is
the zone to watch (App debug menu → Profiler capture — Sebastian captures, not run
headlessly). First levers if it regresses: cell/octave count, or raising `u_rock_min_zoom`.

## Verification

`make -f tests.mk` (sanity only, no shader-relevant tests), `make` + `make DEBUG=1` both
compile clean. Per the project's visual-verification policy, look confirmed live by
Sebastian, not self-verified via headless screenshots — see hand-off in the session that
implemented this.

## Revision history (post-initial-implementation, same session)

The "Implementation" section above describes the **round 1** design as originally planned.
It shipped, then went through three live-feedback rounds the same session — the section
above is now stale on specifics; this log is the accurate record. All rounds stayed within
`assets/scenes/stylus-osm.yaml`'s `rock-texture` style; `elevation.yaml`/`hillshade.yaml`
(the shared `screenAnchoredSunDir()` extraction) were untouched after round 1.

- **Round 1** (as planned): sine-stripe-sum structural noise (`rockCellStripe`/
  `rockErosionOctave`/`rockErosionFbm`). **Feedback: visible moiré, and the pattern read as
  regular "corduroy", not cracks.** Root cause: summing two overlapping *infinite periodic*
  sine fields per octave is a textbook beat-frequency generator, and a folded pure sine is
  perfectly regular by construction.
- **Round 2**: replaced the structural noise with anisotropic Worley/Voronoi F2-F1 (distance
  to 2nd-nearest jittered cell point minus distance to nearest — the standard "cracked mud"
  technique, no periodicity to beat against). Two fixed scales (coarse joints + fine cracks)
  plus a separate multi-octave value-noise grain layer. **Feedback: looked better, but now
  a) too fine relative to the DEM's own visible resolution (fine noise on top of coarse,
  blobby terrain facets), and b) too slow.**
- **Round 3**: `u_rock_scale` changed from a fixed meter value to a **dimensionless
  DEM-texel multiplier** — crack cell size = `u_rock_scale * dxy_elev` (real world-meters-
  per-elevation-texel at that fragment, already sampled for the slope/curvature stencil), so
  the pattern automatically scales with the DEM's actual current resolution (zoom, overzoom
  fallback, source density) instead of a fixed constant. Collapsed to one Voronoi scale (4-
  cell/2×2 shifted-grid search, not the full 3×3, and not two scales) and dropped the
  separate grain-fbm layer entirely (removed the now-dead `u_rock_octaves` uniform/GUI
  slider and `ROCK_TEXTURE_MAX_OCTAVES` define along with it — no more consumer).
- **Round 4**: Sebastian supplied a real profiler trace
  (`assets/profiles/trace-20260730-155714.json`, static camera, Matterhorn z12/tilt0).
  Analysis: `style-gpu:rock-texture` was ~9.3ms/frame mean (up to 33.6ms), steady-state (not
  a panning transient — `zoom`/`pitchDeg`/`visibleTiles` counters were constant across the
  whole trace) — bigger than `style-gpu:unlit-polygons` (3.9ms) and `style-gpu:terrain-
  ground` (3.8ms) *combined*, despite covering less screen area than either (only `bare_rock`
  polygons). Root cause: the bump-normal step was finite-differencing the Voronoi field with
  **2 extra fresh 4-cell searches** (center/dx/dy), tripling the round-3 Voronoi cost for
  something with an exact free alternative. Fix: a Voronoi cell boundary ("crack") is
  geometrically exactly the perpendicular bisector of the two nearest seed points, so
  `normalize(pt2 - pt1)` — from the *same* nearest/2nd-nearest points the one search for the
  visible pattern already finds — **is** the crack-perpendicular bump direction, with zero
  extra cell evaluations. `rockVoronoiEdge`/`rockCrackStructure` now output `crackDir`
  directly; the color block's bump section dropped `eps`/`hCenter`/`hDx`/`hDy` entirely. One
  4-cell Voronoi search per fragment now, total, for both the visible pattern and the bump
  normal (down from round 3's 3 searches). Not yet re-profiled by Sebastian as of this
  writing — expected ~2-3x reduction in the Voronoi-dominated portion of the cost, but that's
  an estimate pending a fresh trace, not a measurement.
- **Round 5**: Sebastian's direction, informed by how game terrain shaders actually get fast
  photorealistic rock (texture splatting + stochastic tiling, not per-pixel procedural
  generation — a bigger architecture change deferred for now) plus two specific references:
  Musgrave's *ridged multifractal* (`Texturing & Modeling: A Procedural Approach`) and a
  faceted-stone SDF codepen (inaccessible to fetch — inference only, from the well-known
  "smooth base + planar facet cuts + fine noise" genre it belongs to, corroborated by
  Sebastian's own framing). Two changes, both same-cost-or-cheaper than round 4:
  - **`rockRidgedMultifractal`** (new function): the *proper* Musgrave technique — our
    pre-round-1 baseline had the ridge fold (`1-abs(2*noise-1)`) but was missing the
    defining step, weighting each octave by the *previous* octave's already-ridged value
    (`n *= weight; weight = clamp(n*gain,0,1)`), which is what produces sharp weighted ridge
    crests with quiet, self-similar-nested valleys — the actual "mountain silhouette" look —
    instead of uniform bumpiness. Plain value noise (`rockNoise`, non-periodic), no cell
    search, 2 octaves. Drives the macro/meso term in the material-weathering blend and
    doubles (inverted) as the lichen-patch mask, replacing that mask's previous separate
    `rockNoise` tap — so its net added cost is smaller than one full new noise layer.
  - **Faceting**: `rockVoronoiEdge`'s bump-direction output changed from round 4's
    `crackDir` (`normalize(pt2-pt1)`, a smoothly-varying gradient) to `facetTilt`, a
    **constant per winning cell** taken directly from that cell's already-computed jitter
    hash (zero extra hash calls — and cheaper than round 4 since it skips the `pt2-pt1`
    normalize). This gives flat-shaded facets meeting at a hard angle change right at the
    crack boundary — real stone/rock angularity, per the codepen reference, comes from
    discrete flat planes, not smooth bump gradients, which read as rounded/organic no
    matter how sharp the underlying crack is. Scaled by `ridgeMask` (full tilt mid-facet,
    relaxing at the crack itself, where the albedo groove already reads as the crack).
  - `u_rock_curv_strength` (dead since round 2 — declared but never referenced) is live
    again, now weighting curvature's contribution to the 3-way weathering blend (crack
    proximity + real DEM curvature + macro ridge/valley).
  - Feedback: still slow, and still looked pixelated (round 5's per-cell flat faceting,
    without real mipmapping, aliases at any distance/oblique angle — the exact class of
    problem every procedural round kept re-fighting).

## Round 6: real texture, all procedural generation removed

Sebastian's call, following the round-5 discussion of how game terrain shading actually
achieves this (texture splatting, not per-pixel procedural generation) — provided a real
scanned rock texture set to try instead of continuing to patch the procedural approach.

**Assets**: Poly Haven "Marble Cliff 05" (CC0), `assets/textures/marble_cliff_05_*_2k.*` -
diffuse (JPEG), normal (`nor_gl`, OpenGL convention), roughness, displacement. Checked
against the engine's actual image loader (`tangram-es/core/src/util/stbImage.cpp` +
`wuffs.h`/`stb_image.h`): JPEG/PNG load fine, but **no OpenEXR decoder exists anywhere in
tangram-es** (only JPEG/PNG/GIF/TGA/PSD via stb_image/wuffs, plus TIFF/LERC for the
elevation-raster path specifically) - the normal and roughness maps, supplied as `.exr`,
would have silently failed to load. Converted both to PNG via ImageMagick, verified
**byte-exact linear passthrough** against two independent sample pixels before trusting it
(a naive EXR→PNG conversion easily bakes in an sRGB gamma curve on the way, which would
have silently corrupted the normal map's encoded vector directions - caught exactly this on
the first attempt with `-colorspace RGB`, which decodes sRGB→linear, the wrong direction;
the correct command is the flag-free default `convert in.exr -depth 8 out.png`). Roughness
PNG converted but not yet wired in (not needed without real specular lighting).

**Wiring**: new top-level `textures:` block (`stylus-osm.yaml`, replacing a long-standing
`#textures:` placeholder comment) declaring `rock_albedo`/`rock_normal` with
`filtering: mipmap` - confirmed via `SceneLoader::loadTexture`/`parseTexFiltering`
(`sceneLoader.cpp`) that this is the flag that actually enables `LINEAR_MIPMAP_LINEAR` +
mip generation, not just a display hint. Referenced from `rock-texture`'s
`shaders.uniforms` as plain scalar strings (`u_rock_albedo_tex: rock_albedo`) - confirmed
via `SceneLoader::parseStyleUniforms` that any uniform value that isn't a valid float/bool/
CSS-color is looked up as a texture name and auto-declared as `uniform sampler2D` in the
generated shader (same mechanism that already worked for `u_rock_shadow_color` etc. as
`vec4`), so no manual GLSL uniform declaration was needed. Texture `url:` paths are
relative to the scene YAML's own directory, confirmed against the existing font-loading
convention already in this file (`../shared/fonts/...`) → `../textures/...`. No `wrap`/
`repeat` option exists in the texture YAML schema at all (checked `loadTexture` - only
`url`/`filtering`/`density`/`sprites`/`size`), so UV wrapping is done manually in-shader via
`fract()` before sampling, rather than depending on GL wrap-mode state the scene file can't
configure.

**Shader**: all procedural crack/ridge/facet/multifractal generation from rounds 1-5
deleted outright (`rockHash2`, `rockCellPoint`, `rockPointDist`, `rockVoronoiEdge`,
`rockCrackStructure`, `rockRidgedMultifractal`, `rockStrata` all gone). Kept only
`rockHash`/`rockNoise` for a single cheap lichen-patchiness tap. UV is plain
`fract(v_world_position.xy / u_rock_scale)` - **not** rotated to the downhill direction the
way the procedural noise coordinate was in every prior round: rotating a real photo's UV
per-fragment shears/warps it, which is fine for an analytic noise function but not for
sampled image data, so the runnel-anisotropy technique from rounds 1-5 doesn't transfer and
was dropped (`u_rock_runnel` uniform/slider removed). `u_rock_scale` reverted from round 3's
DEM-texel-adaptive scheme back to a plain world-meters-per-tile value (that scheme was
specifically about matching procedural facet size to DEM resolution; moot with no
procedural facet grid to mismatch, and mip-mapping now handles the scale/distance mismatch
that motivated it in the first place). Strata/bedding dropped entirely (a real photo
supplies its own structural detail; a synthetic banding layer on top now reads as
redundant/muddying rather than additive). Normal-mapped lighting kept the exact delta-
lighting architecture from rounds 4-5 (macro DEM normal + detail normal combined, lit via
the shared `screenAnchoredSunDir`, with the light term as a *delta* from the unperturbed
macro normal so it can't double up with hillshade's own macro shading) - only the detail
normal's *source* changed, from a procedural approximation to the real normal-map texture,
combined via a standard "whiteout blend" (Barré-Brisebois) since the world-XY planar
projection means tangent space here already **is** the world X/Y/Z basis - no per-vertex
tangent/bitangent reconstruction needed.

Net effect: per-fragment cost is now the 5-tap elevation stencil (always present, pre-dates
this whole effort) + 2 mipmapped texture fetches + 1 cheap noise tap for lichen - well below
every procedural round including round 1, while gaining real hardware-mipmapped
antialiasing (the thing every procedural round's manual `fwidth()` fades were trying and
failing to fully replicate) and actual photographic surface detail.

**Feedback**: texture confirmed loading correctly (`urlClient.cpp`/`scene.cpp` log lines
showed both files fetched) - "just gray" turned out to be mipmap minification at the
zoom-12 test view (a screen pixel there spans several 15m tiles, so the mip chain correctly
blurs toward the texture's average color - not a bug). Zooming in close revealed three real
problems instead.

## Round 7: triplanar projection, scale, and bump depth

1. **Vertical stretching** ("washi paper over a potato mountain"): the round-6 planar XY
   projection is only correct on gentle slopes - on Matterhorn's near-vertical faces it
   smears a small XY range across a huge Z extent, the textbook triplanar-mapping failure
   mode. Fixed with standard triplanar: `macroNormal` (already computed, moved earlier in
   the block since it's now needed sooner) drives per-axis blend weights
   (`pow(abs(macroNormal), 4)`, normalized); three projections (XY top-down, XZ for north/
   south-facing walls, YZ for east/west-facing walls) are sampled and blended for albedo (a
   plain weighted color average, always correct regardless of orientation). The normal map
   is *not* smoothly 3-way blended - each projection's sample needs reorienting into world
   space first (which world axis maps to texture U/V/outward differs per projection), and
   getting a 3-way reoriented blend subtly wrong would be a hard-to-diagnose remote bug,
   so it's simplified to picking the single dominant-axis projection and reorienting only
   that one sample correctly (trades a hard seam at the dominant-axis switch for much lower
   risk of a wrong-but-plausible-looking result). Cost: 6 texture fetches instead of 2 -
   still far cheaper than any procedural round.
2. **Scale** ("I need it to be bigger"): `u_rock_scale` default raised 15 → 50m, GUI slider
   range widened (5-250m, was 2-60) - live-tunable rather than another edit/redeploy round.
3. **Bump depth** ("displacement way deeper"): the `microLight` clamp (`[0.4, 1.6]`) was a
   hard ceiling on perceptible relief *independent of* `u_rock_normal_strength` - no amount
   of increasing that uniform could push past it. Delta amplified 2x, clamp widened to
   `[0.1, 2.5]`, default strength raised 0.8 → 1.8, GUI max raised 2 → 5.

**Feedback**: "weirdly pixelated, and the texture is still tiny and repeating" - a screenshot
showed noisy blotchy static, not a coherently repeating photo (which is what a genuinely
too-small `u_rock_scale` would have looked like - many small but recognizable copies of the
marble photo, not noise).

## Round 8: hardware texture wrapping (real C++ change)

Root cause: `rockUV`/`uvXY`/`uvXZ`/`uvYZ` were `fract()`-wrapped in the shader (rounds 6-7)
because the scene YAML `textures:` block had no way to request `GL_REPEAT` at all -
`SceneLoader::loadTexture` only parsed `url`/`filtering`/`density`/`sprites`/`size`, and
`TextureOptions` defaults to `CLAMP_TO_EDGE`. `fract()` is a hard discontinuity (1.0 → 0.0
every tile), and `texture2D()`'s *automatic* mip-level selection depends on the GPU's
implicit screen-space derivative (`dFdx`/`dFdy`) of the UV - which spikes to garbage right at
every wrap. With small tiles (~50m) across a multi-km alpine view, seams are everywhere on
screen simultaneously, so the LOD selection was garbage everywhere, reading as noise/static
rather than a blurred-but-coherent repeating texture.

Real hardware `GL_REPEAT` wrapping doesn't have this problem: the GPU wraps only the integer
part *during the texel fetch itself*, after derivatives are computed on the continuous
(non-wrapped) coordinate - so mip selection stays correct across tile boundaries by
construction, which is exactly why native texture wrapping exists as a hardware feature
instead of "just `fract()` it yourself" being the standard advice anywhere.

Fix, `tangram-es/core/src/scene/sceneLoader.{cpp,h}`: added `parseTexWrap()` (mirrors
`parseTexFiltering()` exactly) and wired it into `loadTexture()`, so `wrap: repeat` is now a
real scene YAML option (`Texture::generate()` already applied `m_options.wrapS`/`wrapT` to
`GL_TEXTURE_WRAP_S`/`T` correctly - the YAML parsing was the only missing piece). Declared
`wrap: repeat` on both `rock_albedo`/`rock_normal` in `stylus-osm.yaml`, removed the shader's
`fract()` calls entirely (`uvXY`/`uvXZ`/`uvYZ` now just the raw continuous coordinate).
Verified: full Release + Debug rebuild both compile and link clean (this is the first round
of this whole effort needing an actual C++ rebuild - every round 1-7 change was scene/GLSL
only, picked up without recompiling).

**Feedback**: "just as bad" - screenshots showed the marble grain now rendering correctly
(the round 8 fix worked) but with hard-edged, jaggedly pixelated blotchy patches overlaid
across much of the terrain - a different, second bug.

## Round 9: smooth triplanar normal blend, softer lichen

Root cause of the blotches: round 7's normal-map triplanar sampling used a **hard if/else
switch** to the single dominant projection (a deliberate compromise at the time - smoothly
blending 3 reoriented normals seemed riskier to get subtly wrong than picking one, and this
was explicitly flagged as trading a hard seam for lower risk). Real terrain normals flip
which axis "dominates" constantly across ordinary rolling/rocky terrain, not just at extreme
cliff edges - so neighboring fragments routinely landed on different sides of that if/else,
producing sharp, jagged lighting discontinuities every time. Fixed by blending all 3
reoriented normal samples with the same smooth `triW` weights already used for albedo - same
3 texture fetches (no new cost, just a different combination of samples already being
fetched), no more hard switch anywhere in the shader.

Secondary, smaller fix: the lichen patchiness noise's scale was implicitly `u_rock_scale *
4`, so round 7/8 raising `u_rock_scale` 15→50m unintentionally quadrupled lichen patch size
to 200m - coarse enough at zoom-12 overview scale to read as its own hard blocky blotches
(compounding the same visual symptom from a second, independent source). Decoupled to a
fixed 60m scale and widened the `smoothstep` threshold band considerably (soft blob edges
instead of a near-binary on/off cutoff).

Scene/GLSL only, no rebuild needed.

**Feedback**: "still tearing and pixelating in weird ways" plus "still way too small... should
be like covering half of the Matterhorn" - a screenshot (tilted 3D view, steep face) showed
a chaotic torn-paper pattern of dark/light patches with jagged polygonal edges.

## Round 10: soften triplanar blend sharpness, scale by an order of magnitude

Root cause of the tearing: `triW`'s `pow(abs(macroNormal), 4.0)` sharpening. The Matterhorn's
real DEM normal is genuinely jagged at small scale (famous for its four sharp ridges/faces),
and since the XY/XZ/YZ projections sample essentially *uncorrelated* parts of the same photo
(independent UV math per axis), a steep power-4 weight - which snaps the blend to near-binary
the instant one axis edges ahead of the others - turned ordinary jagged terrain into chaotic
high-contrast switching between unrelated texture samples. Round 9's fix (smooth 3-way blend
instead of a hard if/else) was necessary but not sufficient - blending 3 things sharply is
still effectively a switch. Dropped the power to 1 (`triW = abs(macroNormal)`, no extra
sharpening) for a far more gradual blend across real terrain detail.

Scale: Sebastian's "half the Matterhorn" framing meant 50m was still roughly an order of
magnitude too small. `u_rock_scale` default 50 → 400m, GUI slider max 250 → 1000m.

Scene/GLSL only, no rebuild needed.

**Feedback**: "much better" overall. Three follow-ups: (1) asked whether apparent texture
scale could be decoupled from zoom so it stays enjoyable when zoomed out ("or does that look
crazy?"); (2) screenshots showed patches that are just flat gray with no visible texture,
specifically on very steep terrain, correctly self-diagnosed as shadow-related; (3) residual
tearing (vertical striping) still visible on the steepest faces even after round 10.

## Round 11: zoom-scale compensation, contrast pre-boost, stabilized macro normal

1. **Zoom compensation** (not crazy - standard cartographic practice, same category as
   zoom-dependent line widths/label sizes already used throughout this app): a fixed
   real-world tile size necessarily shrinks to imperceptibility at low zoom, correct optics
   but not an attractive map texture. New `scaleZoomFactor = exp2(max(0, 14 -
   u_map_position.z) * u_rock_zoom_compensation)` multiplies `u_rock_scale` as zoom drops
   below 14; `u_rock_zoom_compensation` (new uniform/slider, 0-1, default 0.5) blends between
   pure physical scale (0) and fully screen-constant apparent size (1, exactly cancels
   Web Mercator's per-zoom-level meters-per-pixel doubling).
2. **Gray patches in shadow**: traced to `hillshade.yaml`'s `HILLSHADE_BLEND_OVER` compositing
   - in 3D, hillshade blends a translucent flat-gray macro-shading layer over every
   land-cover polygon (`color = vec4(vec3(gain*L-(gain-1.0)), alpha)`, default alpha 0.55 at
   full tilt). Alpha-blending a flat color over a detailed one mathematically attenuates the
   detail underneath by `(1-alpha)` (~45% of contrast survives), compounded perceptually in
   shadow where the same absolute contrast reads as more washed out. This is the existing,
   heavily-tuned 3D compositing architecture shared by every land-cover type, not a
   rock-texture bug - rock is just the first one with fine spatial detail worth losing to it.
   Not fixable from rock-texture's side (hillshade is a separate style/pass; retuning its
   alpha globally would affect every other land-cover type) - instead pre-boosts contrast on
   `materialColor` before hillshade ever sees it, so more survives: new `u_rock_contrast`
   uniform/slider (1-3, default 1.7), `materialColor = mix(vec3(dot(materialColor,
   vec3(0.333))), materialColor, u_rock_contrast)` (1 = honest no-op).
3. **Residual tearing**: root-caused to `macroNormal`'s own construction, not the triplanar
   blend logic (already fixed in round 10) - finite-difference DEM gradients are numerically
   unstable as terrain approaches vertical (a raster elevation grid can't represent an
   overhang, just a rapid step, and the gradient across that step is highly sensitive to tiny
   horizontal sampling offsets), so `macroNormal` - and therefore which triplanar projection
   dominates - could swing wildly between neighboring fragments on exactly the steepest
   terrain. Fixed by clamping the gradient's magnitude (direction preserved, `8.0 ~=
   tan(83deg)`) before constructing `macroNormal` specifically; `gradTrue` itself
   (slope01/curv01) is untouched.

Scene/GLSL only, no rebuild needed.

**Feedback**: "same shit as before" - a screenshot showed the same torn dark/light blotchy
pattern on steep terrain, visually indistinguishable from before round 11. Also flagged as
"way too slow", with a profiler trace attached.

## Round 12: the real cause was the bump-lighting formula, not triplanar blending

**Perf, resolved by evidence, not by touching anything**: analyzed
`trace-20260801-211128.json`. `style-gpu:rock-texture` is 1.976ms mean - cheaper than
`style-gpu:unlit-polygons` (1.886ms) - confirming texture sampling is in fact well below
every procedural round's cost, exactly as expected. The actual dominant cost in the trace is
`LabelsCollect` (label placement/collision), spiking to **1.79 seconds** on individual
frames - a completely separate, pre-existing subsystem (`[[label-placement-state]]`)
unrelated to any rock-texture work this whole effort. No action taken here; flagged to
Sebastian as a separate investigation if wanted.

**Visual bug, root-caused correctly this time by tracing the actual formula** instead of
reasoning about triplanar geometry again: `microLight`'s delta term has a structural range
of `[-1, 1]` (each `nDotL` independently clamped to `[0,1]`), so round 7's `* 2.0`
amplification combined with the `[0.1, 2.5]` clamp could crush `microLight` all the way to
0.1 - a 10x darkening - whenever the bump-perturbed normal swings away from the sun relative
to the macro normal. On real steep, backlit terrain, combined with the real photographic
normal-map detail (marble grain/veining) at `u_rock_normal_strength` 1.8, this is very
plausibly the actual source of *both* complaints simultaneously: crushed-near-black patches
(not "darker, still texture" the way real shadow should read - directly explaining
Sebastian's "shadow" diagnosis), and a chaotic high-contrast pattern reading as "torn" -
because the normal map's own fine real photographic variation was being amplified this hard,
not because of a triplanar-blend seam (rounds 9-10 fixed a real but apparently secondary
issue). Fix: floor raised **structurally** to 0.5 (at most 2x darkening regardless of how
high `u_rock_normal_strength` is dialed - texture survives by construction, not by picking a
"safe" default), delta amplification reverted 2x → 1x, ceiling trimmed 2.5 → 2.0 to match.
Default `u_rock_normal_strength` pulled back 1.8 → 1.0 - deeper relief should now come from
dialing the (structurally safe) slider up live, not from a default that was unsafe at any
value.

Scene/GLSL only, no rebuild needed.

**Feedback**: "same shit as before" (a screenshot showed the round-12 fix hadn't changed the
visible pattern - see round 13 below for why), plus "way too slow" with a fresh profiler
trace, plus a corrected observation: the perf comparison in round 12's write-up was wrong
(1.976ms is not cheaper than 1.886ms), and a request to reconsider procedural fBm on
performance grounds. Also, a more precise description of the visual bug: torn triangles that
show the plain light-gray underlying elevation surface with literally no texture, worse when
tilted and on steep terrain - explicitly distinguished from (and called more important than)
the shadow-contrast issue round 12 addressed.

## Round 13: perf correction, real label-placement fix (separate subsystem), open investigation

**Perf comparison corrected**: 1.976ms is not cheaper than 1.886ms - `style-gpu:rock-texture`
and `style-gpu:unlit-polygons` are roughly comparable, rock-texture very slightly higher, as
expected (it mixes from unlit-polygons and adds texture sampling on top). Both are far below
the procedural rounds' cost (round 4's trace showed 9.3ms for rock-texture alone) - the data
still doesn't support reconsidering fBm on performance grounds, just not via the specific
(wrong) comparison round 12 made.

**The actual "way too slow" cause, found in the new trace**
(`trace-20260801-211128.json`): `LabelsCollect` (label placement/collision, a completely
separate subsystem) spiking to **1.79 seconds** on individual frames, dominating
`MapUpdate`/`SceneUpdate` (which fully contain it). Root-caused to `Label::refinePriority()`
(labelManager.cpp/label.cpp) having no per-frame cost budget at all, unlike the structurally
similar `Label::refineAnchor()` a few lines below it, which already has one
(`kAnchorRefineBudgetMs`, added in rounds 9/15 of the *label-placement* effort for the exact
same failure mode - "thousands of candidates newly eligible in the same frame" causing
multi-second stalls). Both call into `sampleTextureShading()`
(`util/textureShading.cpp`), whose own single-slot per-mosaic pyramid cache (round 8 of that
same effort) assumes consecutive calls mostly target the same tile - true within one tile's
labels, but the frame that newly streams in many tiles at once (a big pan/zoom, or first
load over the peak-dense Alps) forces a full pyramid rebuild (~600K-element decode + 4
box-filter passes) per tile, for potentially 1000+ peak candidates, every frame until each
succeeds. Fixed by adding the same wall-clock-budget idiom to `refinePriority`
(`kPriorityRefineBudgetMs`, `m_priorityRefineTimeUsedMs`, mirroring the existing pattern
exactly) - real C++ change, both Release and Debug rebuilt clean, tests pass. This directly
satisfies "even if labels show up slowly, we can't have the placement thread slow down the
whole machine": a budget bounds worst-case per-frame cost and lets refinement spread over
however many frames it needs, rather than blocking.

**Torn/textureless triangles**: NOT the same bug as round 12's shadow-crushing fix (that
predicts near-black, not the reported plain light gray - matching `earth_color`/terrain-
ground's own fill, i.e. this reads as the draped polygon not rendering at all in those
triangles, not a lighting/contrast problem). Investigated and ruled out: mesh-chunking
(`PolygonBuilder::chunkOffsets`, oversized-feature index-buffer splitting) rebinding texture
units incorrectly per chunk - texture uniforms are set once per style per frame, not
per-chunk, so this shouldn't be able to cause partial texture loss; and a NaN in round 11's
gradient-clamping at slope-critical points - confirmed structurally safe (multiplies by a
bounded scalar rather than dividing by slope magnitude directly, unlike the OLD `downhill`
computation this codebase already hit that exact bug with once before). Root cause not yet
found. Asked Sebastian whether other draped land-cover types (forest/scree/grass) tear the
same way at the same steep, tilted spots - if yes, this is the older, separately-documented
terrain-drape-mismatch residual (`docs/terrain-drape-cellcaps-investigation.md`), unrelated
to this session's work; if rock-texture-only, still something in this shader specifically.

**Confirmed**: forest tears the same way. This is the pre-existing, general drape-mismatch
issue, not anything from this session's rock-texture work.

## Round 14: torn triangles are `docs/terrain-drape-cellcaps-investigation.md`'s open issue

That document is a separate, deep, 8-round investigation (as of 2026-07-19) into terrain
drape mismatches affecting all draped land-cover styles, currently at "research only, no
fixes implemented... no fix should be started on the strength of this trace alone" per its
own explicit status. Its leading hypothesis: a draped vector tile's composite elevation
target zoom `zt` is hard-capped by the tile's own styling zoom `s`
(`RasterSource::overzoomTargetZoom`), and `s` (a whole-tile aggregate-screen-area heuristic)
can systematically underestimate how fine the elevation tileset is actually displaying under
the screen-area-concentrated part of a *tilted* view - exactly the "worse under tilt, worse
on steep terrain" pattern reported here. The doc explicitly names the consequence: "`rock-
texture`'s slope/curvature reads stay wrong exactly where it matters most" - written before
this session's texture work even existed, about the general mechanism.

Rather than implement the doc's leading candidate fix without confirmation (against its own
explicit caution, and its own track record of "several confident-but-wrong conclusions" over
7 prior rounds), added the exact diagnostic it already specifies and asks for (section 7.1,
`TileManager::upgradeAttachedRasters`, `tangram-es/core/src/tile/tileManager.cpp`) - a
`LOGD` that fires whenever the elevation tileset is displaying tiles finer than a drape
composite's own target zoom, which the existing cap machinery structurally cannot represent
(its walk only goes coarser than `zt`, never finer). Temporary, marked for removal once the
investigation concludes either way. Real C++ change; Release + Debug rebuilt clean, tests
pass.

**Important**: `LOGD` requires `LOG_LEVEL >= 3`; Release builds at `LOG_LEVEL=2`, so this
diagnostic is silent there by design - needs `./build/Debug/ascend` specifically to see any
output, matching the investigation doc's own repro commands (which already used Debug for
this reason).

Suggested repro (doc's own section 7.0, tilt deliberately nonzero - overriding the usual
tilt-0 default specifically for this test, per project convention for diagnostics where tilt
is the point):
```
cd /home/sebastian/projects/maps
./build/Debug/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 16 --view.rotation 0 --view.tilt 45 --sources.last_source stylus-osm-terrain
```
Watch the terminal for `Drape ceiling below displayed terrain: ...` lines while panning/
tilting near the Matterhorn summit and torn triangles are visible. If it fires persistently
while the tearing is visible, the hypothesis is confirmed and the doc's own section 9 lays
out candidate fixes (still requiring sign-off before implementation, per its own policy). If
it never fires during a confirmed repro, the hypothesis is refuted and the doc's section 8
fallback candidates take over.

**Confirmed**: Sebastian ran the repro and the diagnostic fired persistently and heavily
exactly where tearing was visible, e.g. `tile 8540/5829/14/15 zt=15 displayed=z16 (liveS=15
effectiveS=15)` - `zt` capped at `effectiveS` (15) while the elevation tileset was actually
displaying `z16` under that same footprint. Hypothesis confirmed, not refuted.

## Round 15: implemented the composite-target fix (with sign-off)

Investigated the doc's own open `gridRes` coordination question before implementing (its
section 9(a) explicitly says this must be answered as part of the design, not discovered
after): `PolygonBuilder::gridRes` (mesh tessellation density, `polygonStyle.cpp:117-127`) is
fixed once at tile *build* time from that tile's own `id.s`, a completely different lifecycle
from the drape composite's `zt` (recomputed live every `updateTileSets()` pass). Bumping `zt`
alone fixes the *data* the shader reads (color/shading correctness) but not mesh density -
resolved as "accept bounded chording" (the doc's second option) rather than triggering tile
rebuilds to also raise `gridRes`: `terrain_grid: 256` is already generous, and the reported
symptom (wrong/missing composite data, not a geometric hole) is what a data-only fix
addresses.

Implemented in `TileManager::upgradeAttachedRasters` (`tangram-es/core/src/tile/
tileManager.cpp`), presented to Sebastian for explicit sign-off before implementing per the
investigation doc's own policy:
- Hoisted the `rendered`/`ready` elevation-tileset lookup (previously built *after*
  `livePrimary`/`zt`, only for the per-cell cap loop) to *before* `livePrimary` is computed -
  a pure reordering, that block never referenced `livePrimary`/`zt` in the first place.
- Added `finestDisplayedUnderFootprint`: reusing the now-earlier `rendered` set (no new
  tileset walk), find the finest elevation zoom actually displayed anywhere under this tile's
  own (x,y) footprint - independent of `s`/`zt`, which don't exist yet at this point.
- `livePrimary`'s zoom is now `max(effectiveS, raster.tileID.z, finestDisplayedUnderFootprint)`
  instead of just `max(effectiveS, raster.tileID.z)` - can only ever raise the target,
  matching the doc's "errs only in the direction of more fidelity" framing. The existing
  per-cell cap loop further down is untouched and still independently clamps any cell not
  actually covered by finer displayed data back down - this can't fabricate detail, only stop
  discarding real data that was already there.
- The round-14 temporary diagnostic is left in place unchanged and now doubles as a
  post-fix verification signal: it re-derives `zt` from the (now-corrected) `livePrimary`, so
  it should fire far less, or not at all, once this fix is live - if it still fires, that
  points at a second limiting factor (e.g. `overzoomTargetZoom`'s own `maxZoom`/
  `kOverzoomCompositeMaxLevels` ceilings) rather than the mechanism this round addresses.

Real C++ change to core tile/LOD infrastructure shared by every draped land-cover style, not
scoped to rock-texture. Release + Debug rebuilt clean, tests pass (2191 assertions/188 cases,
unchanged). Known risk (per the investigation doc's own analysis, not yet measured): larger
composites and more re-stitches under tilt, additional load in the `hillshade`/
`unlit-polygons` GPU paths that are already the two most expensive things in the app -
aggressive perf work is the explicit next step once this is confirmed to actually fix the
tearing.

**Partially confirmed, then refuted as sufficient**: Sebastian ran the repro. The round-14
diagnostic (`Drape ceiling below displayed terrain: ...`) went quiet as predicted - the
composite-target fix works as designed. But the actual visible symptom (torn triangles,
plain gray showing through on tilted steep terrain) is **unchanged**, in the same places.
This means the round-15 fix addressed a real bug but not the (or not the only) cause of the
originally reported tearing - the round-15 writeup above already named the untested
assumption ("accept bounded chording") that's now the leading suspect.

## Round 16: gridRes-staleness hypothesis - diagnostic added, not yet confirmed

Revisiting the "accept bounded chording" call from round 15: that reasoning assumed
`terrain_grid: 256` gives enough mesh density that coarser tessellation only causes bounded,
minor chording, not visible holes. Re-reading `PolygonStyleBuilder::setup()`
(`tangram-es/core/src/style/polygonStyle.cpp:112-128`) more carefully shows the actual cap is
tighter than that framing suggested:

```cpp
int overzoomLevels = std::max(0, int(id.s) - int(id.z));
m_builder.gridRes = m_style.activeTerrainGrid() > 0
    ? std::min(m_style.activeTerrainGrid(), kTerrainMeshGridRes << overzoomLevels)
    : 0;
```

`kTerrainMeshGridRes = 64` (`tangram-es/core/src/util/builders.h:33`). A tile that was **not**
already overzoomed at the moment it was *built* (`overzoomLevels == 0`, the common case) gets
`gridRes = min(256, 64) = 64`, not 256 - the `activeTerrainGrid()` ceiling only actually
applies once a tile has already been built while overzoomed. And critically, `id.s` here is
the tile's own build-time snapshot, computed once in `PolygonStyleBuilder::setup()` and never
revisited - a completely different lifecycle from `_liveId.s`, the live/current styling zoom
this same `upgradeAttachedRasters` function already receives as a parameter (per its own doc
comment, "visTileId carries the LIVE styling zoom"). Tilting the camera into a close, steep
view of terrain that was cached while more zoomed-out/flat leaves that tile's mesh frozen at
64 cells indefinitely, regardless of what the (now-fixed) elevation composite is doing - a
large flat triangle spanning real relief can miss the terrain surface in patches, which would
look exactly like the reported symptom (plain gray showing through).

Following the same "confirm before fix" discipline used in rounds 14-15 (and the
investigation doc's explicit regression-history warning for this area), added a temporary
diagnostic rather than implementing a fix directly. `TileManager::upgradeAttachedRasters`,
right after the existing `if (_tile.isProxy()) { return; }` check:

```cpp
int32_t builtS = int32_t(_tile.getID().s);
if (int32_t(_liveId.s) > builtS) {
    LOGD("Tile mesh built coarser than live view: tile %s builtS=%d liveS=%d",
         _liveId.toString().c_str(), builtS, int32_t(_liveId.s));
}
```

Fires whenever a tile's mesh was built at a coarser styling zoom than what's currently being
displayed - independent of, and not reusing, the round-14/15 elevation-composite machinery
(this is a mesh-geometry question, not a raster-data question). Same `LOG_LEVEL` caveat as
before: only visible in `./build/Debug/ascend`, silent in Release. Real C++ change; Release +
Debug rebuilt clean (`make` and `make DEBUG=1`, both linked successfully).

Suggested repro (same tilted-Matterhorn approach as round 14, since this is the same visible
symptom being chased):
```
cd /home/sebastian/projects/maps
./build/Debug/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 16 --view.rotation 0 --view.tilt 45 --sources.last_source stylus-osm-terrain
```
Watch the terminal for `Tile mesh built coarser than live view: ...` lines while panning/
tilting near tearing. If it fires persistently where tearing is visible, this is the (or a)
real second cause and a fix (forcing affected tiles to rebuild at a finer `gridRes`, or
decoupling `gridRes` from build-time `id.s` some other way) can be scoped with actual
confirmation behind it. If it doesn't fire, or fires in different locations than the tearing,
the hypothesis is refuted and the search continues elsewhere.

**Refuted**: Sebastian ran the repro. The new diagnostic never fired at all, and the tearing is
still there. This rules out build-time gridRes staleness as the (or a) cause — worth noting
`terrain_grid: 256` on `rock-texture` was already independently "hand-verified sufficient" in
a prior investigation (`stylus-osm.yaml:713-730` comment, referencing that earlier round's own
Matterhorn summit-undershoot testing), so this negative result is consistent with that earlier
finding, not a surprise in hindsight.

Both leading hypotheses for the persisting tearing (composite-target-zoom-only vs.
build-time-frozen mesh density) are now refuted or insufficient. Re-scoping: found a **third,
independently-investigated mechanism** that fits the reported symptom ("literally NO texture...
just the underlying elevation showing through in plain light gray") much more directly than
either of the first two - `docs/rock-texture-elevation-shelf-investigation.md` (2026-07-20,
prior session, not this one). Summary: a vector tile's elevation raster subtask can fail once
(cancellation during pan/zoom churn, or an ordinary network/cache miss - traced as
indistinguishable downstream) and nothing in the codebase ever retries it; the only recovery is
a *passive* per-frame cache-only lookup that either finds the exact texture by luck or walks up
to 3 ancestor levels and pins there *permanently* until something unrelated happens to
repopulate the exact cache slot. A pinned ancestor more than `kOverzoomCompositeMaxLevels` (2)
zoom levels back can never be repaired by the overzoom-composite machinery either (same
`zt = min(maxZoom, primary.s, primary.z + 2)` ceiling this whole rock-texture-v2 investigation
has been circling). The visible result: `getTerrainMeshElevation()` (elevation.yaml) drapes the
polygon mesh onto a stuck, wrong (too-coarse, i.e. too-flat) surface while `terrain-ground`
renders the real one beneath it - exactly "opaque terrain-ground pokes through (flat
background-colored patches)," the same phrase elevation.yaml's own comment (line ~124) uses for
this failure mode.

That earlier investigation already shipped a live diagnostic for this, still present unchanged
(`tangram-es/core/src/tile/tileManager.cpp:672-710`): a `LOGW` (visible in **both** Debug and
Release - `LOGW` only needs `LOG_LEVEL >= 2`, Release's own level, no Debug build required)
that fires once a tile's elevation raster has been continuously stuck missing/pinned for more
than 3 seconds:

```
WARNING: TEMP DIAGNOSTIC (rock-texture-elevation-shelf-investigation.md): '<source>' tile
<x/y/z/s> elevation raster stuck missing/pinned (worst <N> zoom level(s) coarser than
intended) for <T>s -- persistent, not transient
```

Next step (no new code needed yet - reuse the existing diagnostic before writing another one):
run either build over the same tilted-Matterhorn repro and grep the terminal output for "TEMP
DIAGNOSTIC" while panning/tilting near visible tearing:
```
cd /home/sebastian/projects/maps
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 16 --view.rotation 0 --view.tilt 45 --sources.last_source stylus-osm-terrain
```
If it fires with tile ids that line up with where the tearing is visible, this is very likely
the real remaining cause and `docs/rock-texture-elevation-shelf-investigation.md` section 6
already lays out candidate fixes (not yet implemented, needs sign-off per that doc's own
policy). If it stays silent during a confirmed repro, this hypothesis is refuted too and the
search continues.

**Refuted**: Sebastian ran the repro. No such warning fired, and the tearing was unchanged -
and critically, Sebastian reported the tearing happens "even (and particularly) at tilt 0."
This is decisive: every mechanism investigated so far (composite target zoom, gridRes
staleness, elevation-shelf pinning) is either provably inert at `tilt 0`
(`view.cpp:665` returns `FLT_MAX` for every on-screen tile's screen-area at exact nadir,
which is what the composite/gridRes LOD-divergence machinery depends on) or, for the shelf
bug, has no reason to correlate with tilt one way or the other - none of the three predicts
"particularly bad at tilt 0." All three are set aside as not the cause of the reported
symptom (they may still be real, latent bugs worth the diagnostics left in place, but they
are not THIS bug).

## Round 17: root cause found - not an engine bug, a real OSM data coverage gap

Asked Sebastian for a screenshot rather than guess a 5th blind hypothesis. It showed
irregular flat patches with clear relief-shading (contour-following light/dark - hillshade's
macro shading IS present) but no rock-texture detail and, per Sebastian, not even the
`bare_rock` base fill color. First guess - `natural=scree` (a real, separate OSM class with
its own color but never mixed into `rock-texture`) - was tested with a magenta diagnostic
color swap (scene-YAML-only, no rebuild) and cleanly refuted: the patches came back "light
gray," not magenta.

Went to the data directly. Fetched the actual live elevation tiles for this exact location
(z16, the ArcGIS `WorldElevation3D` source configured in `elevation.yaml:33`) and decoded
them with a standalone program linking the vendored LercLib
(`tangram-es/core/deps/lerc/src/LercLib`) to rule out a raw DEM void (a real, well-known
phenomenon on extremely steep alpine terrain in radar-derived elevation datasets, and the
Matterhorn is about as extreme as terrain gets) - `stbImage.cpp:96` already has code
detecting and discarding *fully*-empty LERC tiles as an existing pattern, which made this
worth checking directly rather than assuming. Result: clean, physically plausible data
throughout (min/max in the 3100-4480m range matching the real Matterhorn massif, zero void/
masked pixels, smooth relief in an ASCII heightmap dump of all 9 tiles around the summit).
Ruled out.

Then queried OpenStreetMap's Overpass API directly for `natural=bare_rock`/`scree`/`glacier`
ways in a ~2x3km box centered on the summit and plotted them to scale. **The Matterhorn
summit itself, and most of the surrounding rock faces, are simply not covered by any
`bare_rock`/`scree`/`glacier` polygon in OSM at all** - coverage is sparse, unsurprising for
extremely technical, dangerous-to-survey alpine terrain that gets mapped by volunteers.

This is not an engine or rendering bug. It was invisible before this session's work because
unmapped terrain (`terrain-ground`, `global.earth_color` = `#E0E0E0`) and mapped-but-flat
`bare_rock` (`global.color.rock` = `#E1D9D1`) are both plain, similarly-toned colors with no
internal detail - nothing to visually distinguish "real rock, flatly colored" from "no data
here, showing bare terrain." Giving `bare_rock` a rich photographic material made the
contrast between "textured" and "not" visible for the first time, and reads as the
polygon-shaped mapped areas having "torn holes" where they're actually just... not there.
Explains every piece of evidence collected across rounds 14-17 at once: tilt-independent
(real static vector data), fixed to ground location (permanent, not a load/cache race),
relief-shading present in the gaps (hillshade legitimately shades bare `terrain-ground`,
unrelated to which land-cover polygon if any sits on top of it), and both rock-texture's
accent AND `bare_rock`'s own base fill color genuinely absent (there is no polygon there to
apply either to).

### Candidate directions (not yet decided - Sebastian's call)

1. **Do nothing / accept as an OSM data limitation.** Correct in the sense that the map is
   now accurately reflecting what's tagged; unsatisfying in that the visual "torn" impression
   is real and the whole point of this effort was to make alpine rock look good broadly, not
   only where volunteers happened to trace a polygon.
2. **A DEM-driven procedural fallback**, independent of vector polygon coverage: apply (a
   toned-down version of) the rock treatment to any sufficiently steep/high terrain directly
   from the elevation raster already available to every draped style, regardless of whether
   an OSM `bare_rock`/`scree` polygon exists there. Closest to "fixing" the visual problem at
   its root (coverage gaps stop being visible at all, not just less jarring), but is a
   materially different, larger feature than anything in this doc so far - a new
   slope/elevation-gated style or a change to how `terrain-ground`/`hillshade` itself is
   colored, not a small patch.
3. **Extend/buffer the existing polygon coverage** (e.g., grow `bare_rock`/`scree` polygons
   outward by some margin, or synthesize coverage from a slope threshold at data-prep time)
   - a middle ground, cheaper than (2) but still real new work, and could misclassify terrain
   that's genuinely not rock just because it's adjacent to mapped rock.
4. **Contribute the missing mapping back to OSM** - correct in principle, but far outside
   this codebase/session's scope and not a fix Sebastian can ship in this app.

**Overtaken by round 18's finding below - the OSM-coverage-gap theory was refuted by Sebastian
("nonsense... it's a mesh related issue of some kind"), with a much more precise repro: the
tearing only appears while panning at zoom 16+, pops in at a specific position, and is only
visible from particular viewing angles - never at startup.** That combination (position- and
angle-dependent, but requires panning, not tilt) doesn't match a static data-coverage gap.

## Round 18-19: found and fixed the real cause - a tile-cache-thrashing bug (`TileManager::setCacheSize`)

Chasing the corrected repro, a `PolygonStyleBuilder::setup()` build-time diagnostic
(`polygonStyle.cpp:112-146`, still in place) revealed the actual, much more severe symptom
underlying the tearing: while panning at zoom 16+, Sebastian hit a mode where the exact same
tile (`z=16 s=17`, non-oscillating - confirmed via a full-`TileID` capture) got fully torn down
and rebuilt **37 times in a few seconds**, across an entire ~17-tile visible neighborhood at
once, making the app "absurdly slow" and effectively unusable. This is a distinct, more
fundamental bug than anything chased in rounds 14-17 - full tile rebuilds (earcut +
`buildPolygonGrid` re-tessellation + GPU upload) are expensive and should only happen once per
tile, not every frame; the intermittent, position/angle-dependent tearing is very likely a
side effect of this churn (a tile briefly has no mesh, or a stale/incomplete one, in the window
between eviction and rebuild completion).

Root-caused via a temporary `TileManager::addTile()` diagnostic logging cache hit/miss and
occupancy on every call: `cacheUsage` was pinned essentially at its ceiling
(`~179-184MB / ~185MB`) throughout, confirming genuine cache-capacity thrashing - tiles were
being evicted (LRU) and immediately re-needed, forcing constant re-fetch+rebuild. But the
configured budget (`mapsapp.cpp:818`, `cfg()["tile_cache_limit"]`, default 512MB) doesn't match
either the observed ~185MB ceiling or `TileManager`'s own `DEFAULT_CACHE_SIZE` (32MB,
`tileManager.h:35`) - tracing `TileManager::setCacheSize()` (`tileManager.cpp:1402-1407`
before this fix) found the actual bug:

```cpp
void TileManager::setCacheSize(size_t _cacheSize) {
    m_maxCacheLimit = _cacheSize;
    if (m_tileCache->cacheSizeLimit() > _cacheSize) {
        m_tileCache->limitCacheSize(_cacheSize);
    }
}
```

`TileCache::limitCacheSize()` is the *only* function that updates `TileCache::m_cacheMaxUsage`
(the actual enforced limit) - and this guard only calls it when *shrinking*
(`current limit > new size`). `TileCache` is constructed at the 32MB default
(`tileManager.cpp:155`); `mapsapp.cpp`'s startup call to raise it toward 512MB is therefore a
silent no-op every time, since 32MB is never greater than the new, larger target. The real
enforced limit could only ever shrink from 32MB, never actually reach the intended budget.
(The observed ~185MB ceiling - between the 32MB construction default and the intended 512MB -
isn't fully explained by this alone and wasn't chased further once the fix was in place and
confirmed against the actual failure mode; worth another look if the same guard turns out to
have a second call site not yet found.)

**Fix** (`tileManager.cpp:1402-1417`): removed the guard, always call
`m_tileCache->limitCacheSize(_cacheSize)` - correct in both directions (shrinking still evicts
down to the new limit; growing just raises the limit, and the eviction `while` loop inside is a
no-op when usage is already under it). Real C++ engine change, not scoped to rock-texture -
affects every source's tile cache. Release + Debug rebuilt clean, `make -f tests.mk` passes
(2191 assertions/188 cases, unchanged).

Also re-enabled round 15's `finestDisplayedUnderFootprint` fix, which had been temporarily
disabled mid-investigation to test (and rule out) whether *it* was the cause of this loop -
confirmed not the cause (the loop persisted identically with it disabled), and leaving it off
had simply unmasked the original round-14 "Drape ceiling below displayed terrain" composite-
mismatch bug again (confirmed firing heavily live once disabled) - so it's restored to the
round-15 formula.

Not yet confirmed by Sebastian whether this actually resolves the "absurdly slow"/looping
behavior and the torn-holes symptom - real next test.
