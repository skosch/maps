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
