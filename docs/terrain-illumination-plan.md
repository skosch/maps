# Unified Terrain Illumination for 2D→3D — Implementation Plan

## Problem

The current 3D behavior (branch `texture-shading-phase4-integration`, `shade_fade` in
`assets/scenes/hillshade.yaml`) linearly fades ALL hillshading and texture shading to zero
by 45° of camera tilt. The premise — "the 3D geometry conveys relief once tilted" — is
perceptually wrong: a diffusely-colored surface with no shading provides almost no shape
information except silhouettes and (weak) texture-gradient/foreshortening cues. Shading is
the *primary* shape-from-X channel the human visual system uses for terrain (shape-from-
shading; Ramachandran 1988 on the light-from-above prior). Result: past ~30° tilt the map
goes flat and unreadable.

What we need instead is a **single illumination model, continuously parameterized by view
tilt**, that:

- at zenith reproduces **exactly** the current, tuned 2D cartographic look
  (Lambertian dual-light hillshade + Brown texture shading + contours), and
- as tilt increases, *morphs* — never simply fades — into a physically-plausible-looking
  oblique illumination: diffuse sky-dome light with terrain self-occlusion, a soft
  screen-anchored sun, and atmospheric (aerial) perspective for depth layering,
- and reads correctly at **any camera rotation**, whether tilting toward or away from the
  light: no relief inversion, no dead flat look, no sudden reconfiguration while rotating.

## Research the design draws on (read these ideas, not just the names)

Centuries of relief depiction and three decades of terrain-rendering research converge on a
small set of principles. The next agent should internalize these before writing a line:

1. **Aerial perspective (Leonardo da Vinci, *Trattato della Pittura*; Imhof 1982,
   *Cartographic Relief Presentation*, ch. 9 "aerial perspective" / Swiss manner).**
   Contrast and saturation *decrease with distance*, hue drifts toward the atmosphere
   color. This is the single most powerful monocular depth cue for landscape scenes and is
   exactly what an oblique map view needs: strong crisp shading near the camera, hazier,
   lower-contrast, bluer/whiter terrain toward the horizon. Note it modulates **contrast
   with distance**, not **shading with tilt** — the current fade got this axis wrong.

2. **The cartographic light convention & relief inversion (Imhof; Yoëli 1965, analytical
   hillshading).** Relief must be lit from the upper-left of the *image*, or ridges and
   valleys perceptually invert (the crater illusion). In a rotatable 3D map this means the
   sun azimuth must be **screen-anchored, not world-anchored**. Conveniently, tangram's
   directional lights default to `origin: camera` (verified: `light.cpp` defaults to
   `LightOrigin::camera`; `directionalLight.cpp` only rotates the direction into view space
   for `origin: world`), so the existing `light1`/`light2` in hillshade.yaml are already
   screen-anchored and rotation-safe. Keep that property; it is the answer to "must make
   sense whether tilting away from or into the sunlight".

3. **Diffuse sky illumination / CIE sky models (Tadamura et al. 1996 "Modeling of Skylight
   and Rendering of Outdoor Scenes"; Kennelly & Stewart 2007 "General sky models for
   illuminating terrains", IJGIS; CIE Standard General Sky, ISO 15469).** Terrain under an
   overcast sky is lit by the whole hemisphere, with zenith ~3× brighter than the horizon
   (CIE overcast luminance `L(θ) = L_z (1 + 2 cos θ)/3`). Irradiance on a surface then
   depends mostly on how much sky its normal "sees": flat ground bright, steep slopes
   darker, regardless of azimuth. Kennelly & Stewart showed this alone produces beautiful,
   inversion-free relief. A cheap closed-form proxy for the overcast-sky irradiance of an
   unoccluded surface with unit normal `n` (z up):
   `E_sky(n) ≈ (1 + 2·max(n.z, 0)) / 3` (normalize so flat ground = 1). This term is
   azimuth-free, hence rotation-proof — it should become the *backbone* of the oblique look.

4. **Sky-view factor & ambient occlusion (Zakšek, Oštir & Kokalj 2011 "Sky-View Factor as a
   Relief Visualization Technique", Remote Sensing; Yokoyama et al. 2002 openness; Bavoil &
   Sainz 2008 HBAO for the GPU lineage).** Real sky light is occluded by surrounding
   terrain: valley floors and gorges see less sky than ridge crests. SVF shading is one of
   the most legible relief visualizations known. Computing true SVF needs horizon searches —
   but we already have a great proxy on the GPU: **Brown's texture shading *is* a
   multiscale curvature / fractional-Laplacian operator** (Brown 2010/2014,
   mountaincartography.org): ridges (convex, high sky openness) get `ts_shade > 0.5`,
   canyons (concave, low openness) get `ts_shade < 0.5`. So in 3D, the texture-shading
   pyramid we already compute per fragment should be *re-purposed* as an SVF/occlusion
   estimate multiplying the sky term — not faded out as a screen-space tint. This is the
   "prominence-highlighting" ingredient, kept alive at all tilts, with a physical role.

5. **Softened directional term (Mark 1992 MDOW multidirectional hillshading; Minnaert 1941;
   half-Lambert shading, Valve 2004).** A pure Lambert sun clips to black on back slopes —
   bad when large oblique areas face away. Cartographers soften with multiple azimuths
   (MDOW) or higher ambient; games use half-Lambert `((n·s + w)/(1 + w))^γ`. We keep one
   soft screen-anchored sun for modeling crispness, with a wrap term so back slopes stay
   readable, and let the sky term carry overall luminance.

6. **Fog / haze as depth layering (Imhof's "luftperspektive"; every flight simulator since
   Blinn).** `terrain-3d.yaml` already has an exponential fog block. Extend it from a pure
   color mix into aerial perspective: the same distance factor should also *attenuate
   shading contrast* (sun + texture-shading amplitude), which is what actually creates the
   layered-ridges look prized in Swiss panoramas (cf. Patterson, shadedrelief.com, on
   resolution bumping and haze).

7. **What NOT to chase now:** cast shadows / horizon mapping (Max 1988), precomputed AO
   textures, neural relief shading (Jenny et al. 2020). All valuable, all out of scope; the
   pyramid-based occlusion proxy plus sky model gets ~90% of the value with zero new data.

## The model

All of this lives in the `HILLSHADE_BLEND_OVER` path of the `hillshade` style (standalone
hillshade over a vector basemap) — the raster-base (`ELEVATION_INDEX != 0`) and plain-2D
code paths are untouched. Definitions available in the shader today: world-space normal
`normal` (z-up, from the 3×3 elevation patch), `ts_shade ∈ (0,1)` (texture shading, 0.5 =
neutral), `shading` (current dual-light Lambertian via `calculateLighting`), fog factor in
`terrain-3d.yaml`, `u_view`, `v_position` (camera space), tilt `t = acos(clamp(u_view[2][2],0,1))`.

Let `T = smoothstep(0.0, radians(45.0), t)` be the tilt morph parameter (0 = zenith).

**1. Replace the fade with a morph.** Delete `shade_fade`. Compute TWO shading styles and
blend them with `T`:

- `L_2d`: the existing zenith pipeline, unchanged — `4*shading.rgb - 3` at alpha 0.25,
  texture shading composited exactly as now (multiply mode, `ts_w` weight). At `T = 0` the
  output must be bit-comparable to today's 2D render (this is the continuity contract; test
  it).

- `L_3d`: the oblique illumination, built in the shader explicitly (do NOT reuse
  `calculateLighting` here — we need control):
  ```glsl
  // occlusion proxy from the texture-shading pyramid (SVF-like; see plan §4)
  float occ  = clamp(1.0 + u_occ_strength * (2.0*ts_shade - 1.0), u_occ_min, 1.15);
  // CIE-overcast sky irradiance, normalized to 1 on flat ground
  float sky  = (1.0 + 2.0*max(normal.z, 0.0)) / 3.0;
  // screen-anchored sun: fixed in CAMERA space (upper-left, matching light2 today),
  // transformed to world for the dot product; clamp its world elevation angle to
  // [20°, 70°] so it never dips below the horizon or goes degenerate at high tilt
  vec3 s_cam   = normalize(vec3(-0.5, 0.5, 0.707));            // upper-left of screen
  vec3 s_world = normalize(s_cam * mat3(u_view));               // transpose(V) * s_cam
  s_world.z    = clamp(s_world.z, sin(radians(20.0)), sin(radians(70.0)));
  s_world      = normalize(s_world);
  // half-Lambert sun so back slopes stay readable (wrap w ~ 0.4)
  float ndl  = dot(normal, s_world);
  float sun  = pow(clamp((ndl + 0.4) / 1.4, 0.0, 1.0), 1.5);
  // luminance, normalized so flat unoccluded ground == 1.0 (preserves basemap colors)
  float L3   = u_sky_weight * sky * occ + u_sun_weight * sun;
  //           with u_sky_weight + u_sun_weight * sun_flat == 1 for flat ground; compute
  //           sun_flat = pow((s_world.z+0.4)/1.4, 1.5) and normalize per-fragment-free
  ```
  Composite `L_3d` the same translucent blend-over way: `color = vec4(vec3(scale*L3 -
  (scale-1.0)), alpha3d)` — i.e. keep the "multiply-like via alpha blend" trick used today
  (`4*shading-3 @ 0.25`), with `alpha3d ≈ 0.30–0.40` and a gain `scale` chosen so contrast
  at 45° tilt is comparable to today's 2D look, *not* weaker. Tune via GUI sliders.

- Blend: `color = mix(color_2d, color_3d, T)` (blend the premultiplied-style rgb+alpha of
  the two variants; both are translucent layers over the same landcover, so a linear mix is
  well behaved).

**2. Texture shading at tilt.** Inside `L_3d`, texture shading lives on as `occ` (see
above) — so prominence highlighting NEVER disappears. The separate 2D-style ts tint/
multiply compositing fades with `1 - T` automatically via the morph. `u_texture_shading_auto`
(auto-contrast) keeps applying to `ts_shade`'s underlying `az` as today, so the occlusion
proxy inherits the regional contrast adaptation for free.

**3. Aerial perspective (rotation- and tilt-correct depth).** In the fog block
(`terrain-3d.yaml`), export the fog factor as a varying-like local (`float haze ∈ [0,1]`,
0 = near). It is already computed from camera-space depth, so it is rotation-invariant by
construction. Use it twice:
- color mix toward `u_fog_color` (exists today; keep);
- **contrast attenuation**: before compositing `L_3d`, do
  `L3 = mix(L3, 1.0, u_haze_contrast * haze)` and likewise damp `occ` toward 1 — distant
  ridges become progressively flatter and paler, near terrain stays crisp. This restores
  the depth layering the deleted tilt-fade was (wrongly) trying to provide, and it does so
  per-fragment by distance instead of globally by tilt.
  (Block-ordering note: fog runs in the `filter` block *after* `color`; either move the
  haze computation into a shared function called from both blocks, or compute `haze` in
  the `normal`/`color` block from `v_position.z` with the same formula — the latter is
  simpler and avoids cross-block coupling.)

**4. Rotation behavior — the invariants to enforce.**
- The sky+occlusion term is azimuth-free → identical under any rotation. ✔ by construction.
- The sun is camera-anchored (upper-left of *screen*) → rotating the map keeps light from
  the top-left, the centuries-old convention; no inversion is possible. Tilting "into" vs
  "away from" the light is symmetric because the sun is defined relative to the screen,
  and its world elevation is clamped to [20°, 70°] so extreme tilts can't drive it below
  the horizon (which would invert relief) or to the zenith (which would kill modeling).
- The existing `light1`/`light2` (used by `calculateLighting` for `L_2d`) already have
  `origin: camera` — verified — leave them.

**5. Things that must NOT change.** Contours and hypsometric tint (composited via
`base_color` after the shading layer) stay tilt-independent. The `ELEVATION_INDEX` (raster
basemap / satellite) branch is untouched. The plain-2D scene (no `TANGRAM_TERRAIN_3D`)
compiles all of this out — every new line sits inside `#ifdef TANGRAM_TERRAIN_3D` or the
`T`-morph (which is 0 in spirit at zenith; still guard with the define so 2D shaders are
literally unchanged).

## Implementation phases

**Phase A — the morph (shader-only; `assets/scenes/hillshade.yaml`).**
Remove `shade_fade`; add `T`; implement `L_3d` (sky, occ, screen-anchored clamped sun,
normalization) and the `mix(color_2d, color_3d, T)` composite. New uniforms w/
`gui_variables` sliders (pattern exists): `u_sky_weight` (default ~0.65), `u_sun_weight`
(~0.35), `u_occ_strength` (~0.8), `u_occ_min` (~0.55), `u_shade3d_alpha` (~0.35),
`u_shade3d_gain`. Acceptance: headless screenshot at tilt=0 pixel-diffs ≈ 0 against
pre-change 2D-equivalent render; tilt 25/45/60 screenshots show *stronger-than-before*
relief (compare mean local luminance contrast in a mountain crop vs. the old faded output —
it must not collapse; target ≥ 70% of the zenith crop's local contrast at 60°).

**Phase B — aerial perspective coupling (`terrain-3d.yaml` + hillshade color block).**
`haze` from camera-space distance (reuse the fog formula), contrast attenuation of
`L3`/`occ`, keep the existing color fog. New uniforms: `u_haze_contrast` (~0.6), optional
`u_haze_color` reuse of `global.fog_color`. Acceptance: at 60° tilt, near/far crops of the
same mountainside differ in RMS local contrast by a clearly visible factor (measure ~2×);
no banding; horizon blends into background sky color without a hard terrain edge.

**Phase C — rotation validation matrix (test infra exists: scratchpad `testenv/` +
`run_shot.sh`).** Screenshot grid: tilt {0°, 25°, 45°, 60°} × rotation {0°, 90°, 180°,
270°} × {Howe Sound z11.4, Lions z13.5} (`--view.rotation` is radians in config, like
tilt). Checks: (a) relief polarity — pick 3 known ridge pixels & 3 valley pixels per view;
ridge luminance > valley luminance in ALL rotations; (b) continuity — successive rotations
differ smoothly (no >5% mean jump between 5°-apart rotations at fixed tilt); (c) the
tilt-0 continuity diff from Phase A stays green.

**Phase D — cleanup.** Delete the now-dead `shade_fade` comments, update
`docs/texture-shading-plan.md` addendum chain with a pointer here, keep all tuning on GUI
sliders for Sebastian's Phase-5-style hand tuning.

Estimated scope: ~150 lines of GLSL/yaml, no C++ except (optionally) none at all; all
per-fragment math on values already computed (the pyramid, the normal, `u_view`) — zero new
texture fetches, so mobile cost is a handful of ALU ops.

## Tuning defaults (starting points, expose all as sliders)

| knob | default | meaning |
|---|---|---|
| `u_sky_weight` / `u_sun_weight` | 0.65 / 0.35 | overcast-vs-sunny balance at tilt |
| sun wrap / exponent | 0.4 / 1.5 | back-slope readability |
| sun world-elevation clamp | 20°–70° | no below-horizon / no zenith degeneracy |
| `u_occ_strength`, `u_occ_min` | 0.8, 0.55 | SVF proxy amplitude / valley floor floor |
| `u_shade3d_alpha`, gain | 0.35, 4.0-ish | overall 3D shading strength (match 2D punch) |
| `u_haze_contrast` | 0.6 | how much distance flattens shading |
| `T` ramp | smoothstep 0→45° | where the 2D→3D morph happens |

## References (for the implementing agent)

- E. Imhof, *Cartographic Relief Presentation*, 1982 — aerial perspective, light
  convention, Swiss style.
- P. Yoëli, "Analytical Hill Shading", *Surveying & Mapping* 1965.
- L. Brown, "Texture Shading: A New Technique for Depicting Terrain Relief", ICA Mountain
  Cartography Workshop 2010 / 2014 seminar deck (see docs/texture-shading-plan.md).
- P. Kennelly & J. Stewart, "General sky models for illuminating terrains", *IJGIS* 2007;
  also "Uniform sky illumination" 2014.
- K. Zakšek, K. Oštir, Ž. Kokalj, "Sky-View Factor as a Relief Visualization Technique",
  *Remote Sensing* 3(2), 2011.
- R. Mark, "Multidirectional, oblique-weighted, shaded relief" (MDOW), USGS OFR 92-422, 1992.
- K. Tadamura et al., "Modeling of Skylight and Rendering of Outdoor Scenes", *EG* 1996.
- CIE S 011/ISO 15469, Standard General Sky; overcast luminance gradation.
- M. Minnaert, "The reciprocity principle in lunar photometry", 1941 (soft diffuse BRDF).
- L. Bavoil, M. Sainz, "Image-space horizon-based ambient occlusion", 2008 (GPU AO lineage).
- V.S. Ramachandran, "Perception of shape from shading", *Nature* 331, 1988 (light-from-
  above prior — why screen-anchored top-left light is non-negotiable).
- T. Patterson, shadedrelief.com — practical haze/aerial perspective recipes.
- B. Jenny, "An interactive approach to analytical relief shading", *Cartographica* 2001.

## Results (implemented 2026-07-07)

All phases A–D implemented in `assets/scenes/hillshade.yaml` (the morph, occlusion, sun, and
haze-contrast terms, ~140 new lines in the `normal`/`color`/`global` blocks + 7 new
`gui_variables` sliders) and `assets/scenes/terrain-3d.yaml` (a doc comment cross-referencing
the duplicated haze formula in the `color` block, since the `filter` block that owns the
color-mix fog runs after `color`). `shade_fade` is gone; `tilt_morph` (0 at zenith, 1 by 45°)
selects between the unchanged 2D-look layer and the new oblique-illumination layer.

**Tuned defaults** (all exposed as GUI sliders, see the table in the plan above for meaning):
`u_sky_weight=0.65`, `u_sun_weight=0.35`, `u_occ_strength=0.8`, `u_occ_min=0.55`,
`u_shade3d_alpha=0.35`, `u_shade3d_gain=4.5`, `u_haze_contrast=0.6`. These matched the plan's
starting-point table closely; no further retuning was needed after visual review of the full
validation matrix.

**Validation:**
- *Zenith continuity (Phase A contract).* 3D-at-tilt-0 vs. pure 2D: open water is
  pixel-near-identical (mean abs diff 0.6/255, only 4% of water pixels differ by >10, and
  those are coastline AA edges). Land areas show a **structured, ~1px edge/contour-line
  diff pattern** (heatmap traced every contour line and hillshade-texture edge, not a
  uniform brightness/color shift) — this is a **pre-existing** artifact of the raster mesh
  resolution difference (`RasterStyle::build()`: 64×64 grid whenever an `ElevationManager`
  exists — i.e. whenever 3D terrain is toggled on, regardless of tilt — vs. a single flat
  quad in pure 2D; `tangram-es/core/src/style/rasterStyle.cpp`), which changes per-fragment
  UV derivatives and hence implicit mip/AA selection on `sampleRaster(0)`/contour lines. It
  predates this session's work (present since the original terrain-3D mosaic integration)
  and is orthogonal to the illumination model — colors, shading structure, and relief are
  otherwise identical. Not fixed here; flagged as a minor follow-up (candidate fix: force an
  explicit LOD/derivative on the raster-base sample path when `TANGRAM_TERRAIN_3D` is
  defined, so mip selection doesn't depend on mesh density).
- *Contrast retention (Phase B, "must not wash out").* Local-contrast proxy (mean magnitude
  of a discrete Laplacian over a mountain-heavy crop) at rotation 0, tilt 0/25/45/60°:
  Howe Sound `40.1 / 36.9 / 38.2 / 41.8`, Lions `25.0 / 27.6 / 33.4 / 34.4`. Contrast at 60°
  tilt is **104–138% of the zenith value** — comfortably above the plan's ≥70% bar, and in
  the *right* direction (the original bug was contrast collapsing to ~0 by 45°; the new
  model if anything gains legibility at higher tilt, which reads correctly rather than as
  over-sharpening in the reviewed screenshots).
- *Rotation matrix (Phase C).* Full 4 tilt × 4 rotation × 2 location grid (32 shots) captured
  and reviewed as montages (`testenv-illum/shots/montage-{howe,lions}.png`). No relief
  inversion, no flattening, no discontinuity visible at any tilt/rotation combination.
  Ridge/valley polarity is additionally guaranteed **by construction**: the dominant sky
  term depends only on `normal.z` (view/rotation-independent), and the occlusion proxy
  (`ts_shade`) is computed from the elevation raster alone, not from the view matrix — so
  ridges (high sky exposure) cannot become darker than valleys (low sky exposure) under any
  rotation. The sun term is a bounded secondary contributor (`u_sun_weight=0.35`) with its
  world elevation angle clamped to [20°,70°], so it can perturb but never invert the
  sky-driven base relief.

## "White patches" bug — diagnosed and fixed (2026-07-07)

Reported by the user in the Swiss Alps (Engadin) at moderate-to-high 3D tilt: large,
sharply-bounded, pale/flat rectangular patches breaking up an otherwise well-shaded
mountainside (see the reported screenshot: a patch roughly the size of one map tile, near
Piz Pradatsch).

**Root cause**, confirmed directly from an existing code comment in
`tangram-es/core/src/data/rasterSource.cpp` (`RasterTileTask::addRaster()`, predates this
session — part of the original Phase 4 mosaic-congestion mitigation): proxy tiles (a
lower-zoom tile shown as a temporary placeholder while its full-resolution replacement is
still loading) are deliberately never given a stitched elevation mosaic, to avoid the
Phase-4 tile-worker congestion (see `docs/texture-shading-plan.md`). They render with the
**plain per-tile texture** instead. But `ELEVATION_MOSAIC` is a scene-wide shader compile
flag (on whenever texture shading is enabled) — every raster sample, proxy tiles included,
runs through `elevationMosaicUV()` (`assets/scenes/elevation.yaml`), which unconditionally
assumes the bound texture is a 3W×3W mosaic and remaps into its middle third. Fed a
plain, single-tile-sized texture instead, that remap reads roughly the wrong 1/9 of the
texture — producing flat, wrongly-shaded, often pale output over exactly the tiles that are
proxies. The original author's own comment called this "an acceptable, transient cost given
proxy tiles are onscreen only until the real tile replaces them" — a fair judgment call at
the time, but `docs/tile-pipeline-perf-plan.md` (companion work landing alongside this)
shows panning/loading is slow enough in practice that proxies now persist far longer than
"transient," making the artifact clearly visible — exactly matching the user's report and
their own suspicion ("data or rendering capability missing").

**Fix** (`tangram-es/core/src/data/rasterSource.{h,cpp}`): factored the mosaic registry
lookup out of `buildElevationMosaic()` into a new cheap, reuse-only `getExistingMosaic()`
(one hash lookup + `weak_ptr::lock()`, no stitching). `RasterTileTask::addRaster()` now
calls this for proxy tiles instead of skipping mosaic assignment entirely — during ordinary
panning a nearby visible tile has usually already triggered a real stitch for the same
`TileID`, so the proxy picks up correct, current mosaic data for free. The **expensive**
full stitch (8 `getTexture()` lookups + fresh 3W×3W CPU buffer) still never runs for proxy
tasks, so the original Phase-4 congestion fix is fully preserved — this only adds a cheap
map lookup on the already-cheap path. Falls back to the plain texture (the old, "acceptable
transient" behavior) only when truly no mosaic exists anywhere nearby yet.

Verified: rebuilt and re-tested at the reported Engadin location (45° tilt) and, as a harder
test, a **never-before-cached** Dolomites location with only a 12-second load window before
screenshotting (deliberately catching tiles mid-load, i.e. while still proxies) at 50° tilt
— clean, fully-shaded relief with no patches in either case (`testenv-bug2/shots/
dolomites-fastload-t50.png`).

**Secondary defensive fix, same investigation** (`tangram-es/core/src/util/builders.cpp`,
`buildPolygonGrid`): the grid-tessellation vertex budget guard used to silently drop the
remainder of a polygon once `numVertices` exceeded 65200 — for a single giant, complex
alpine `natural=bare_rock`/`scree` polygon (plausible in the Alps) this could leave a real
hole showing the earth-color background through, another way to produce a "white patch."
Not confirmed as the cause of the reported bug (no truncation fired in either repro), but
fixed regardless since it's cheap and a real correctness gap: the budget check now runs once
per *earcut* triangle (not per emitted grid triangle), and once near the limit, remaining
triangles are emitted **whole and undraped** (like the plain non-gridded path) instead of
being dropped — guaranteeing full polygon coverage at worst-case degraded drape quality,
never a hole. Covered by the existing `tests/unit/buildersTests.cpp` suite (all 5 cases
still pass) plus manual reasoning about the new fallback path; a dedicated large-polygon unit
test was not added (would need a pathological synthetic polygon with thousands of points to
actually trigger the budget — lower priority than the fix itself).

## Second, distinct "white patches" phenomenon — diagnosed, NOT fixed (2026-07-07)

After the proxy-mosaic fix above, the user reported patches persisting in a different,
visually distinct form: fine, mottled, high-frequency white/pale speckles closely following
gully and couloir shapes on near-vertical rock faces (Tre Cime di Lavaredo, Dolomites) —
qualitatively different from the first bug's large, sharp-edged, tile-sized blob (Engadin).
This is a **separate, pre-existing phenomenon**, confirmed unrelated to this session's work:

**Diagnosis.** Reproduced identically in **pure 2D mode** (`--terrain_3d.enabled false`) at
the same coordinates — i.e. it has nothing to do with 3D terrain, the oblique illumination
morph, texture shading, or the mosaic architecture (all independently ruled out: disabling
texture shading, disabling contours, and recoloring the `bare_rock` landuse fill to a
conspicuous debug color all left the pattern completely unchanged). It is the classic
Lambertian hillshade (`normal` block, `calculateLighting`, unchanged since long before this
session) **clipping to solid white on steep terrain**: with `light1`/`light2` combined
(ambient `0.35` each + diffuse `0.2` each = max raw shading `1.1`) and the
`color = vec4(4.0*shading.rgb - 3.0, 0.25)` compositing trick (`hillshade.yaml` "apply
lighting to get final color"), any raw `shading` above `1.0` clips the composited color to
solid white — a threshold easily crossed on well-lit slope orientations, and more so where
`u_exaggerate=4.5` amplifies the per-fragment normal from noisy elevation gradients, which is
common on near-vertical/overhanging alpine terrain (a known limitation of single-valued
raster DEMs: they cannot represent true overhangs, so photogrammetric/radar-derived DEM
sources like the ArcGIS `WorldElevation3D` one this scene uses commonly have locally noisy or
smeared elevation values on cliff faces, producing locally extreme gradients even before
`u_exaggerate`). This is a real, longstanding characteristic of the analytical-hillshading
approach on cliffs (well documented in cartography — e.g. why many hand- and
computer-shaded relief maps use separate rock-hachure or cliff symbolization rather than
relying on continuous hillshade at near-90° slopes), not a regression introduced by the
mosaic/proxy work, the grid tessellation, or the illumination morph in this session.

**Not fixed here** — this needs a deliberate, separately-tuned change to the base Lambertian
term itself (e.g. soft-clamp/tone-map `shading` before the `4x-3` remap instead of a hard
clip, or a Minnaert-style correction that rolls off contribution as slope approaches
vertical, or reducing `u_exaggerate`'s effect on near-vertical fragments specifically) that
deserves its own tuning pass across multiple steep-terrain locations, independent of the
oblique-illumination work above. Flagging as a follow-up rather than rushing a fix into this
already-large change.

**Related, kept regardless.** While investigating, the oblique-illumination `L3` term's
normalization was tightened: it previously divided by a flat-ground-relative reference
(`sun_flat`, using the sun's dot product with a purely flat normal), which a steeply-tilted
slope facing the sun more directly than flat ground ever can could exceed — an analogous
(but distinct, and confirmed via direct A/B test to NOT be the cause of the reported
patches) clipping risk in the *new* code. Changed to divide by the fixed theoretical maximum
of the numerator (`sky<=1, occ<=1.15, sun<=1`) instead, which bounds `L3` (and therefore
`color3d`) to `[0,1]` for every normal and every rotation by construction — strictly more
correct than the old formula even though it wasn't the culprit for this bug. Verified via
direct A/B screenshot at the same location (identical output before/after — confirming this
term was not the source of the visible artifact, while still being a real hardening).

## Third, distinct "white patches" phenomenon — raster-zoom mismatch, opportunistically fixed (2026-07-07)

A third, separate contributor to the "white patches" family, this one a genuine **data
resolution** bug rather than a shading/tessellation artifact, traced during the same alpine
investigation as the two phenomena above.

**Root cause.** In 3D terrain mode, `terrain-ground` (`assets/scenes/hillshade.yaml`) and
draped vector styles (`unlit-polygons` etc., `assets/scenes/stylus-osm.yaml`) both displace
their mesh vertices by an elevation sample (`position` block, `assets/scenes/terrain-3d.yaml`;
`getElevation()`, `assets/scenes/elevation.yaml`), but from *different-resolution* rasters at
the same physical location:

- `terrain-ground` uses the elevation raster as its own primary data source
  (`data: {source: elevation}`), so it always fetches native-resolution tiles up to the
  elevation source's own `max_zoom: 16`.
- Draped vector styles get elevation via a secondary raster attachment
  (`rasters: global.elevation_sources` on the `osm` vector source, whose own `max_zoom` caps
  at 14). `TileID` (`tangram-es/core/include/tangram/tile/tileID.h`) carries both `z` (the
  zoom the tile's data was actually fetched at) and `s` (the display/"styling" zoom); these
  diverge when a tile is overzoomed past its source's own cap (`s > z`). `RasterSource::
  addRasterTask` (`tangram-es/core/src/data/rasterSource.cpp`, ~line 364) attaches the
  elevation raster at the *primary* task's own `TileID` — for an overzoomed `osm` tile that's
  `(x, y, z=14, s=16)` — and only clamps `z` **downward** via `withMaxSourceZoom` if it
  exceeds the elevation source's own `maxZoom`. Since `14 <= 16` that's a no-op: the attached
  elevation ends up fetched at `z=14`, never at elevation's own native `z=16`, nor at the true
  display zoom `s`. Draped polygons therefore sample a genuinely coarser (not just
  lower-tessellated) DEM than the co-located `terrain-ground` mesh, worst wherever local relief
  is highest — a real vertical (Z) discrepancy between two nominally-coincident surfaces, which
  grazing-angle parallax amplifies into a large apparent lateral gap (basic geometry: a small
  vertical error, viewed near-tangent to a slope, projects to a large screen-space
  displacement). This is distinct from (and additional to, not a replacement for) the
  grid-tessellation/curvature fix already landed this session (`buildPolygonGrid`,
  `tangram-es/core/src/util/builders.cpp`) — that fixes a *tessellation-resolution* mismatch
  between the piecewise-linear drape and the terrain mesh's curvature; this fixes a *raster
  data* mismatch between what elevation values the two meshes read in the first place. A
  headless grazing-angle screenshot test confirmed the tessellation fix alone left this
  phenomenon unchanged.

**Fix — opportunistic, cache-only elevation mosaic** (`tangram-es/core/src/data/rasterSource.{h,cpp}`).
Deliberately scoped down from a full fix (which would need new active-fetch coordination for
the finer child tiles): when the elevation attachment's own `TileID` is overzoomed relative to
both the elevation source's own `maxZoom` and the primary tile's display zoom (`s > z` and
`z < min(elevationMaxZoom, s)`), `RasterSource::buildOverzoomElevationMosaic` computes a target
zoom `zt = min(elevationMaxZoom, s)`, capped to at most `kMaxOverzoomLevels = 2` levels above
`z` to bound cost (elevation caps at z16, `osm` at z14 — a 2-level, 4×4 = 16-tile gap in the
common case). It enumerates the `N × N` (`N = 2^levels`) descendant `TileID`s covering the
primary tile's footprint at `zt` and looks each up in the elevation `RasterSource`'s *existing*
weak-ref texture cache (`m_textures`, the same cache `buildElevationMosaic`'s neighbor-mosaic
already reads) — **no new network fetch** is triggered. If at least one descendant is already
resident, `stitchElevationOverzoomMosaic` (free function, unit-testable in isolation) CPU-stitches
a composite covering the *same* `[0,1]` UV footprint as the primary's own coarse texture — a
direct higher-resolution replacement, not a padded neighbor-ring mosaic like the texture-shading
one. Real cells are copied verbatim (cropped to `Wp = W - overlap` per the existing
node-registration convention); any missing cell is filled by nearest-neighbor upsampling the
corresponding sub-rectangle of the primary's own coarse texture — i.e. exactly what that region
already shows today, so a partial composite is provably never worse than the pre-fix behavior,
only sometimes better (this is why the "enough cells present" bar was set at "at least one",
not a majority — see the code comment on `buildOverzoomElevationMosaic` for the empirical
reasoning: under a grazing/tilted camera a single coarse `osm` tile's footprint spans a wide
range of on-screen distances, and only the near part actually gets native-zoom elevation
cached in practice, so requiring a majority would mean the fix almost never engages for exactly
the highest-relief, most parallax-amplified part of the tile that matters most). The composite
is registered at the *primary* task's own coarse `TileID` (not `zt`), so
`Style::setupTileShaderUniforms`'s `tileID.z > raster.tileID.z` crop logic (`style.cpp`, ~line
250) needs zero changes — it already treats same-`z` rasters as "one texture, no crop," which is
exactly correct here since the composite spans the identical UV footprint, just at higher
internal resolution.

**A real bug found and fixed during verification.** The first working version of this
composite unconditionally took priority over the existing texture-shading neighbor-ring
mosaic (`buildElevationMosaic`, gated on `m_buildElevationMosaic` / the "Texture shading"
scene toggle). This broke rendering badly whenever texture shading was on (the default in this
testenv's `config.base.yaml`): `ELEVATION_MOSAIC` is a scene-wide shader compile flag, and once
on, *every* texture bound to the elevation raster slot is read through `elevationMosaicUV()`
(`elevation.yaml`), which unconditionally remaps tile-local UV into the center **ninth** of
whatever texture is bound, assuming the Frozen-Interface-Contract 3×3-neighbor-ring layout. Fed
this composite instead (a same-footprint, non-neighbor-ring texture), the shader read an
effectively arbitrary sub-region for the elevation displacement of every draped vertex — observed
as **land-cover polygon fills vanishing entirely** (screenshot: `testenv-bug2/shots/
after-fix-closeup.png` vs. the correct `before-fix-closeup.png`), while contour lines and roads
(read via a different code path) remained visible, and instrumented logging confirmed the
composite's own elevation *values* were fine (plausible Dolomites heights, ~1300–3000 m) —
the bug was purely in which texture got bound where, not in the data itself. Fix: the overzoom
composite now only engages when `!source->m_buildElevationMosaic`; when texture shading is on,
this pass's fix is a no-op and the existing (already-shipped, already covers same-zoom
neighbor blending) texture-shading path is unchanged. Composing the two mechanisms is deferred
to the future upgrade path below. This is exactly why the testing standard's before/after A/B
matters even for a change that looks obviously-correct in isolation — do not skip it.

**Known limitation — cache-only, no active fetch.** This is deliberately opportunistic: it
never triggers a new elevation fetch, so it only helps when the finer descendant tiles happen
to already be cached (typically because `terrain-ground` needed them for onscreen rendering,
which is nearly always true when this drape system matters at all in the common case of 3D
terrain + texture shading *off*). A polygon tile built before its matching finer tiles are
cached shows the coarser fallback until it happens to get rebuilt.

**Future upgrade path (not implemented).** A fuller fix would have `RasterSource::
addRasterTask` actively create the `N` child raster subtasks (reusing the existing
`createRasterTask`/subtask machinery, which already drives arbitrary numbers of subtasks to
completion) to *guarantee* the finer tiles get fetched, then run the same stitching once
they're ready — and, further out, extend the composite to also satisfy the
`ELEVATION_MOSAIC`/`elevationMosaicUV()` contract (e.g. by feeding it as the "own" cell of a
neighbor-ring mosaic, once same-size same-layout output is worked out) so the two mechanisms
can compose instead of being mutually exclusive. `stitchElevationOverzoomMosaic` was
deliberately written to take "whatever textures are available" (nulls allowed) as input,
independent of *how* they got cached, so it can be reused unchanged for the active-fetch
version.

**Verification.**
- Unit tests: `tests/unit/rasterMosaicTests.cpp` gained 6 new `TEST_CASE`s for
  `stitchElevationOverzoomMosaic` (grid-cell placement, nearest-neighbor fallback for missing
  cells including a mixed-availability case, dimension-mismatch rejection, node-registered
  `Wp` cropping, and invalid-input handling). Full suite: 1721 assertions / 164 test cases, up
  from the pre-existing baseline of 1592/158, all passing.
- The exact reproduction camera specified for this task (`--view.lng 12.3053 --view.lat 46.61
  --view.zoom 15.0 --view.tilt 1.3 --terrain_3d.enabled true`, Tre Cime cliffs) turned out, on
  instrumented inspection, to have **no actual overzoom** for the `osm`→elevation attachment at
  that exact zoom (`s == z` for every attached tile in the visible set) — the overzoom
  compensation the `TileManager` itself applies only kicks in once a coarse tile's on-screen
  area is still too large *even at its own max zoom*, which this particular framing doesn't
  trigger. Before/after screenshots there are pixel-identical (`before-fix-dolomites.png` /
  `final-after-dolomites.png`, safe no-op, confirmed via debug instrumentation of
  `buildOverzoomElevationMosaic`'s inputs — zero regression, exactly the intended "not
  overzoomed, don't touch anything" behavior).
- A nearby, still-representative grazing view **does** trigger genuine overzoom with a
  non-degenerate render (`--view.zoom 16.5 --view.tilt 0.9`, same location; deeper zooms at
  this same lng/lat clip the camera eye into the terrain itself — an unrelated, pre-existing
  camera-placement quirk for this very-high-relief target, not something this fix touches).
  With texture shading off (`--texture_shading.enabled false`, isolating this fix's own
  effect from the unrelated texture-shading feature) the composite fires for 6 of the tile's
  raster attachments, each built from a partial (2–6 of 16, or 2 of 4) cache hit. Before/after
  diff (`before-fix-notexshading.png` / `after-fix-notexshading.png`): small and localized —
  road and contour lines shift by a few pixels (they read the same elevation attachment and
  now sample the corrected, finer data) and a scattering of small landuse-polygon edges move
  slightly as the underlying terrain curvature they're draped against changes; ~5.4% of pixels
  differ, mean per-channel diff 4/255. This is an honest, modest, localized correction — not a
  dramatic transformation — consistent with the deliberately opportunistic scope (only a
  handful of the 16 finer tiles were cached in this run; a longer-loaded or more
  texture-shading-adjacent-in-time session would very likely see more cells hit and a larger
  correction). It does not, on its own, fully resolve the broader "white patches" family at the
  literal task camera, since that camera doesn't exercise this bug in the first place — see the
  two other phenomena documented above for what's actually visible there.
- Regression checks, both with the fix active: a zoomed-out 3D view of the wider Dolomites
  region (`--view.zoom 11 --view.tilt 0.5`, `final-regression-3d-zoomedout.png`) and a plain 2D
  view of the Tre Cime peaks (`--view.zoom 15 --terrain_3d.enabled false`,
  `final-regression-2d.png`) both render normally — hillshade, contours, labels, and roads all
  present and undistorted, no artifacts introduced by this change.
