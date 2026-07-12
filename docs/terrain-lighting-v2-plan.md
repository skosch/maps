# Terrain Lighting v2 — Implementation Plan

Successor to `docs/terrain-illumination-plan.md` (the 2D→3D morph, implemented 2026-07-07).
That work fixed "shading fades to nothing at tilt." This plan fixes what it left behind:

1. **BUG: the 2D light is world-anchored NW, not screen-anchored** — rotating the map
   rotates the light with the terrain, violating the light-from-upper-left convention the
   whole design depends on. (Root cause verified below; the old plan's claim that
   `origin: camera` made the lights screen-anchored was wrong.)
2. **Tilted 3D views flatten mountain faces into uniform gray** — the dominant
   illumination term is azimuth-free, the sun term is weak/over-wrapped, everything is
   normalized against a theoretical max that real terrain rarely reaches, and the overlay
   is achromatic.
3. No cast shadows — the single biggest missing shape cue for oblique mountain views.

Target: one illumination model, continuously parameterized by tilt, screen-anchored
upper-left sun (= exactly the swisstopo/Imhof look at north-up zenith), warm/cool
chromatic shading, a proper tone shoulder instead of ad-hoc clipping, and mip-accelerated
cast shadows from the elevation mosaic.

All work on branch `texture-shading-phase4-integration` (worktree
`.claude/worktrees/phase4-integration`, tangram-es submodule on same-named branch).
Do NOT merge to master without Sebastian's approval.

---

## Part I — Verified facts about the current code (read before implementing)

Every claim here was verified against the code on 2026-07-10. File paths are relative to
the phase4-integration worktree; line numbers are as of that date (re-locate by the quoted
code if lines have shifted).

### F1. The NW-light bug: exact mechanism

- `assets/scenes/hillshade.yaml` bottom (`lights:` section, ~line 590):
  ```yaml
  light1: { type: directional, direction: [0, 0, -1], diffuse: 0.2, ambient: 0.35 }
  light2: { type: directional, direction: [1, -1, -1], diffuse: 0.2, ambient: 0.35 }
  ```
  No `origin:` key → defaults to `LightOrigin::camera` (`tangram-es/core/src/scene/light.cpp`).
- `tangram-es/core/src/scene/directionalLight.cpp:36-44` (`setupProgram`): the direction
  is rotated by the view normal matrix **only for `origin: world`**. For `origin: camera`
  (the default) the raw yaml vector is passed to the shader **untransformed**. So the yaml
  direction is meaningful only if the normal it is dotted against is in *camera* space.
- `tangram-es/core/shaders/polygon.fs:69-92`: the standard pipeline normal `v_normal` IS
  camera-space (`polygon.vs:125`: `v_normal = normalize(u_normal_matrix * worldNormal())`).
  But the hillshade style's `#pragma tangram: normal` block **overwrites** `normal` with a
  **world-space, z-up** normal: `normal = normalize(vec3(-hscale*grad/coslat, 1.))`
  (`hillshade.yaml` normal block, ~line 398).
- `tangram-es/core/shaders/directionalLight.glsl:20`:
  `nDotVP = clamp(dot(_normal, -_light.direction), 0,1)` — a world-space normal dotted
  against a raw yaml vector. **Net effect: the yaml direction is interpreted in world
  space.** `light2`'s `-direction = normalize(-1, 1, 1)` = from west+north+up = **NW at
  35.26° elevation, fixed in world space**. Rotating the map rotates the light with the
  terrain. This is the bug Sebastian observed. (`light1` is the zenith light — direction
  `(0,0,-1)` is azimuth-free, so it is unaffected by the space mixup.)
- At **north-up rotation** the camera axes coincide with world axes (camera x = east =
  screen right, camera y = north = screen up), so the buggy behavior and the intended
  screen-anchored behavior are **pixel-identical at rotation 0**. The fix only changes
  rotated views. This gives Phase 1 a free exactness contract.

### F2. Who else uses the scene lights — do NOT retune light1/light2

`grep -n "lighting:" assets/scenes/*.yaml`: every style is `lighting: false` **except**
`heightglow` (`assets/scenes/stylus-osm.yaml:377`, `lighting: vertex`) — the building
style — and the hillshade style itself (no `lighting:` key → fragment lighting default).
Lights are scene-global: changing `light1`/`light2` values would alter building shading.
Therefore: **leave the `lights:` section untouched** and stop using `calculateLighting`
in the hillshade style; compute lighting explicitly in the shader blocks instead.

### F3. The exact current 2D shading formula (the parity contract)

Material defaults are white (`tangram-es/core/src/style/material.h:130-138`: ambient =
diffuse = vec4(1)), specular is not enabled for this style. `calculateLighting`
(`tangram-es/core/shaders/lights.glsl`) with the two lights above therefore reduces to:

```
raw  = 0.7                                     // 0.35 + 0.35 ambient
     + 0.2 * clamp(normal.z, 0, 1)             // light1, zenith
     + 0.2 * clamp(dot(normal, S_nw), 0, 1)    // light2, S_nw = normalize(-1, 1, 1)
sh   = clamp(raw, 0, 1)                        // final clamp in calculateLighting
                                               // (raw can reach 1.1 → the clamp matters)
```
then in the yaml color block (~line 487):
```
over = max(sh - 0.95, 0);  sh -= over - over/(1 + 3*over)   // soft-clip shoulder
color = vec4(4*sh - 3, 0.25)                                // translucent blend-over
```
then texture shading multiplies on top. Any refactor must reproduce this **exactly** at
tilt 0 / rotation 0 (`sh` is also reused as `contour_color = shading*contour_color`,
~line 414 — keep that working).

### F4. Uniform availability (2D included)

`tangram-es/core/src/style/style.cpp:229-232` sets `u_normal_matrix`,
`u_inverse_normal_matrix`, `u_view`, `u_proj` **unconditionally for every style, every
frame** — 2D or 3D, terrain on or off. So a shader-only fix works in plain 2D. Camera→world
for a direction: `s_world = s_cam * mat3(u_view)` (GLSL `v*M` = `Mᵀv` = inverse rotation;
this exact pattern is already used and validated in the 3D block, hillshade.yaml ~line 561).
The view matrix has no scale, so `mat3(u_view)` is a pure rotation.

### F5. The current 3D (tilted) model and why it looks flat gray

`hillshade.yaml` color block, `TANGRAM_TERRAIN_3D && HILLSHADE_BLEND_OVER` section
(~lines 527-585): `tilt_morph` (0 at zenith → 1 at 45°) mixes the 2D layer toward:
```
L3 = (u_sky_weight * sky * occ + u_sun_weight * sun) / (u_sky_weight*1.15 + u_sun_weight)
     sky = (1 + 2*max(normal.z,0))/3          // CIE overcast — depends ONLY on normal.z
     occ = texture-shading SVF proxy           // multiplies sky ONLY (correct, keep)
     sun = halfLambert(dot(n,s), wrap=0.4)^1.5 // screen-anchored, elev clamped [20°,70°]
color3d = vec4(vec3(4.5*L3 - 3.5), 0.35)       // achromatic gray overlay
```
Defaults `u_sky_weight=0.65 / u_sun_weight=0.35`. Four compounding flatteners:
1. 65% of the light is azimuth-free (`sky` sees only `normal.z`) — every steep face gets
   the same value regardless of aspect. Aspect is what separates mountain faces.
2. The sun is 35%, wrap 0.4 compresses its range ~1.4×, exponent 1.5 flattens midtones.
3. Normalization by the *theoretical* max (`sky·occ = 1.15` needs flat ground AND max
   ridge bonus simultaneously; `sun = 1` needs a slope dead-facing the sun) pushes all
   realistic values into a narrow midtone band.
4. The overlay is literally gray (`vec3(L3)`), and `u_haze_contrast=0.6` then removes
   contrast over most of an oblique frame.

Good parts to keep: occ multiplies sky only (games' "AO on ambient, never on direct"
rule); the sun-elevation clamp; haze; the morph guaranteeing zenith continuity.

### F6. Elevation sampling for the shadow march

`assets/scenes/elevation.yaml`, `get-elevation` style:
- `getElevationAtLod(vec2 uv, float lod)` (~line 212, gated `ELEVATION_MOSAIC` +
  fragment/vertex-rasters): meters; `uv` is **tile-local [0,1]**; it does the mosaic UV
  remap internally (`elevationMosaicUV`, ~line 75) and internally caps lod so the bilinear
  footprint stays inside the 3×3 neighbor ring (`maxLod = log2(Wp) - 0.585`, ~line 223).
- The remap `(uv*(Wp+overlap) + Wp)/msize` is linear ⇒ **tile-local uv in ≈[-1, 2] is
  valid** and lands in the neighbor-ring cells with real neighbor data. So a shadow ray
  can march up to ~one full tile width in any direction — no more. (Mip levels cover the
  same spatial footprint at lower res; they don't extend reach.)
- The mosaic exists only when texture shading is enabled (`global.elevation_mosaic`,
  `ELEVATION_MOSAIC` scene-wide define). **Cast shadows therefore require texture shading
  on** — acceptable; gate everything `#ifdef ELEVATION_MOSAIC`.
- Useful values already computed in the hillshade `normal` block: `elev` (2nd-order
  interpolated fragment elevation, ~line 392), `dxy_elev` (projected meters per elevation
  texel, vec2, ~line 379), `coslat` (Mercator scale factor: true meters = projected
  meters × coslat... note the code divides grad by coslat — horizontal true meters =
  projected × coslat, ~line 383), `texwh` (texels per tile after mosaic correction,
  ~line 349).

### F7. Test harness

Prior sessions' harness (warm mbtiles caches for Howe Sound/Lions and Dolomites):
`/tmp/claude-1000/-home-sebastian-projects-maps/10fd84a2-e237-4685-b29e-a08549bb0ef4/scratchpad/`
contains `testenv-illum/` (rotation-matrix work, `run_shot.sh`, `config.base.yaml`,
`shots/montage-{howe,lions}.png`) and `testenv-bug2/`, `testenv2/`. `run_shot.sh` runs
`build/Debug/ascend` under Xvfb (`LIBGL_ALWAYS_SOFTWARE=1`), waits `WAIT` (default 30 s),
screenshots via `xdotool key shift+Print`, quits via `ctrl+q`, saves `shots/<name>.png`.
**If /tmp was wiped**: recreate by copying a surviving testenv, or build fresh: a dir with
`config.base.yaml` (copy from an old testenv — it sets cache paths + scene), symlinks
`res`/`scenes`/`shared`/`plugins` → worktree `assets/`, and the `run_shot.sh` above with
`TESTDIR`/`WT` paths fixed (it hardcodes both — sed them). View args pass through:
`--view.lng --view.lat --view.zoom --view.tilt --view.rotation` (radians)
`--terrain_3d.enabled true|false --texture_shading.enabled true|false`.
Build: `make linux DEBUG=1 LOG_LEVEL=0 -j$(nproc)` in the worktree (Debug is fine for
screenshots; use Release only for perf judgment). Unit tests: `make -f tests.mk`.

Standard test views:
- **Howe Sound** z11.4 (broad relief): `--view.lng -123.25 --view.lat 49.5` (exact values
  in `testenv-illum/shots/` filenames / old configs — reuse whatever the montages used).
- **Lions** z13.5 (local relief).
- **Dolomites / Tre Cime** `--view.lng 12.3053 --view.lat 46.61 --view.zoom 15` (cliff
  stress test — the white-patches location).

### F8. GUI slider pattern

`gui_variables` in `hillshade.yaml`'s `application:` block: entries with `style:
hillshade` bind a uniform to a live slider (`min/max/step/label`), `type: color` gives a
color picker (see `contour_color`). Every new tuning knob in this plan gets a slider —
Sebastian hand-tunes. Uniform defaults live in `styles.hillshade.shaders.uniforms`.

---

## Part II — Phases

Each phase is independently landable, has an explicit acceptance test, and must leave
`make -f tests.mk` green (no C++ changes are expected until Phase 4's optional bits, but
run it anyway) plus a clean app build.

### Phase 0 — Harness + baselines (½ session)

1. Restore/rebuild the testenv per F7 into this session's scratchpad; verify one shot
   renders (`run_shot.sh smoke --view.zoom 11.4 ...`).
2. Capture the **baseline matrix** with the *current* code:
   - 2D (terrain off): Howe + Lions, rotation {0°, 45°, 90°, 180°, 270°}, tilt 0.
   - 3D: Howe + Lions, tilt {0°, 25°, 45°, 60°} × rotation {0°, 90°, 180°, 270°}.
   - Dolomites z15 tilt 45° (cliff/clip stress).
   Name them `base-<loc>-t<tilt>-r<rot>.png`. These are the A/B references for every
   later phase. Keep them for the whole project.
3. Write a tiny `diffstat.py` (or reuse prior sessions' approach): mean |Δ| per channel +
   % pixels with |Δ|>10, over a PNG pair. Used for the parity contracts.

### Phase 1 — Screen-anchor the 2D light (the bugfix)

**Change** (`assets/scenes/hillshade.yaml` only):

1. In the `normal` block, replace the `calculateLighting` call (blend-over branch,
   ~line 412) with the explicit equivalent, using a camera-space sun rotated to world:
   ```glsl
   // screen-anchored sun: upper-left of the SCREEN at 35.26° — identical to the old
   // world-NW light2 at north-up rotation, correct (screen-fixed) under rotation
   vec3 s2_cam = normalize(vec3(-1.0, 1.0, 1.0));
   vec3 s2_world = s2_cam * mat3(u_view);   // camera→world (see F4)
   float raw2d = 0.7 + 0.2*clamp(normal.z, 0.0, 1.0)
                     + 0.2*clamp(dot(normal, s2_world), 0.0, 1.0);
   vec4 shading = vec4(vec3(clamp(raw2d, 0.0, 1.0)), 1.0);
   ```
   Keep the variable named `shading` (vec4) so `contour_color = shading*contour_color`
   and the color block's `shading.rgb` reads are untouched.
2. Do NOT touch the `lights:` section (F2 — buildings use it). The unused
   `calculateLighting` definition still compiles; the existing
   `#undef TANGRAM_LIGHTING_FRAGMENT` at the end of the color block must stay (it
   prevents polygon.fs:92 from applying lighting a second time).
3. Decide-and-do for the raster-basemap branch (`#if ELEVATION_INDEX` … `#else color =
   calculateLighting(...)` in the color block, ~line 493 — hillshade over satellite):
   apply the same explicit formula there (`color = vec4(shading.rgb * base_color.rgb,
   1.0)` using the same `raw2d`… note this branch used `calculateLighting(_,_,
   base_color)` = shading × base_color with white material — the explicit product is
   exact). Same parity argument applies.

**Pitfalls**
- One subtlety in world-space y: tile-local raster v and world north — irrelevant here
  (the normal is already world-space and correct; only the light vector changes), but
  relevant in Phase 4; noted there.
- In plain 2D with the camera tilted but terrain off (tangram allows this), `mat3(u_view)`
  includes the pitch; `s2_world` then has elevation ≠ 35°. Acceptable (matches how the 3D
  path behaves), but don't be surprised in tests.
- `u_view` in a **vertex-lighting** context doesn't matter here (hillshade lighting is
  fragment); don't move this code into a vertex block.

**Acceptance**
- `base-*-t0-r0` (2D and 3D-at-zenith, rotation 0) vs. new shots: mean |Δ| ≈ 0 (allow AA
  noise; use the diffstat from Phase 0; target <0.5/255 mean, matching the previous
  zenith-continuity methodology).
- Rotated 2D shots (r45/r90/r180/r270): shading must now be **screen-fixed** — e.g. at
  r180 the bright flanks that were on the SE side of ridges in the baseline must now be
  on the screen-upper-left side. Visual check on the montage + the ridge/valley polarity
  check from the old plan's Phase C (3 ridge + 3 valley pixels per view, ridge > valley
  in all rotations).
- 3D rotation matrix unchanged in character (the 3D sun was already screen-anchored).

### Phase 2 — One tilt-parameterized model; kill the flat gray

**Goal**: replace the "two models mixed by tilt_morph" architecture with **one function
whose parameters interpolate with tilt**, and retune the tilted endpoint to be
sun-dominant with honest normalization + a real tone shoulder. This is the phase that
fixes "flat gray faces."

**Design** (all inside the existing `#if defined(ELEVATION_MOSAIC)…` /
`HILLSHADE_BLEND_OVER` structure of hillshade.yaml; new helper in the `global` block):

```glsl
// Unified terrain luminance. T = tilt_morph (0 zenith .. 1 by 45°).
// At T=0 this must reduce ALGEBRAICALLY to the Phase-1 2D formula.
float terrainLuma(vec3 n, vec3 s_world, float occ, float sunvis, float T) {
    float w_amb = mix(0.7,  u_amb_weight_3d,  T);   // default u_amb_weight_3d  ~0.25
    float w_sky = mix(0.2,  u_sky_weight,     T);   // default u_sky_weight     ~0.30
    float w_sun = mix(0.2,  u_sun_weight,     T);   // default u_sun_weight     ~0.45
    float wrap  = mix(0.0,  u_sun_wrap,       T);   // default u_sun_wrap       ~0.2
    float sky   = clamp(n.z, 0.0, 1.0);             // NOTE: n.z, not CIE (see below)
    float sun   = pow(clamp((dot(n, s_world) + wrap)/(1.0 + wrap), 0.0, 1.0),
                      mix(1.0, u_sun_gamma, T));    // u_sun_gamma ~1.2 (was 1.5)
    // occlusion multiplies sky only; shadow multiplies sun only (Phase 4; sunvis=1 now)
    float L = w_amb + w_sky * sky * mix(1.0, occ, T) + w_sun * sun * sunvis;
    // normalize so FLAT UNOCCLUDED ground == 1 (not the theoretical max — that was the
    // dynamic-range killer, F5.3). Flat ground: sky=1, sun=sun_flat(s_world.z).
    float sun_flat = pow(clamp((s_world.z + wrap)/(1.0 + wrap), 0.0, 1.0),
                         mix(1.0, u_sun_gamma, T));
    return L / max(w_amb + w_sky + w_sun * sun_flat, 1e-3);
}
```
- Dropping the CIE `(1+2 n.z)/3` form for plain `n.z` is deliberate: with a separate
  ambient weight they span the same affine family (`CIE = 1/3 + 2/3·n.z` is just
  ambient+slope), and `n.z` makes the T=0 reduction exact. Document this in a comment.
- **Sun vector**: one definition for all tilts — `s_cam = normalize(vec3(-1,1,1))`
  (35.26°, exactly Phase 1's), then the existing world-elevation clamp [20°,70°]
  (~line 562) applied at all T (at zenith the clamp is a no-op: elevation is 35°).
  Delete the old separate `s_cam = normalize(vec3(-0.5,0.5,0.707))`.
- **Tone pipeline — RESOLVED 2026-07-10, exact T=0 parity by construction.** Key
  algebraic fact: the old 2D soft-clip `over = max(sh-0.95, 0); sh -= over -
  over/(1+3*over)` IS the rational shoulder `y = s + (m-s)·o/(o+(m-s))` with `s = 0.95`,
  `m = 0.95 + 1/3` (check: `over/(1+3·over) = (1/3)·o/(o+1/3)`). So instead of choosing
  between "normalize and lose parity" vs. "don't normalize and lose headroom",
  interpolate all three protection stages with T; every stage reduces to the old 2D code
  exactly at T=0:
  ```glsl
  L  = w_amb + w_sky * sky * mix(1.0, occ, T) + w_sun * sun * sunvis;
  L /= mix(1.0, w_amb + w_sky + w_sun * sun_flat, T);   // flat-ground norm; off at T=0
  L  = min(L, mix(1.0, u_lum_cap, T));                  // old hard clamp at T=0; ~off at
                                                        //  T=1 (u_lum_cap default 2.0)
  float m = mix(0.95 + 1.0/3.0, u_shoulder_max, T);     // u_shoulder_max default 1.0
  float o = max(L - 0.95, 0.0);
  L  = L - o + (m - 0.95) * o / (o + (m - 0.95));       // == old softclip at T=0
  // haze contrast attenuation MUST be tilt-gated: L = mix(L, 1.0, u_haze_contrast*haze*T)
  //  (haze = 0.0 outside TANGRAM_TERRAIN_3D). haze is NOT ~0 at zenith with terrain on
  //  (fragments sit ~eyeh away -> haze ~0.4); the old code only got away with applying it
  //  ungated because it lived inside the tilt-mixed 3D layer. occ's own haze attenuation
  //  needs no extra gate - occ's effect is already T-gated via mix(1.0, occ, T).
  ```
  Why `u_shoulder_max = 1.0` at T=1: post-gain white is `gain·L-(gain-1) = 1` exactly at
  `L = 1`, so with the shoulder asymptoting to 1.0, pure white is approached but never
  reached over an area — sun-facing steep slopes stay differentiated (the Dolomites
  white-patch guard, replacing the old fixed-theoretical-max normalization). No separate
  `u_shoulder_start` uniform — 0.95 is correct at both endpoints; add a slider later only
  if tuning demands it.
- **Gain/alpha**: interpolate `gain = mix(4.0, u_shade3d_gain, T)`,
  `alpha = mix(0.25, u_shade3d_alpha, T)`; one composite path
  `color = vec4(vec3(gain*L - (gain-1.0)), alpha)` replaces both the 2D and 3D copies.
- **Single-layer pitfalls (added with the resolution above):**
  - The 2D texture-shading tint/multiply compositing currently fades at tilt *for free*
    via the final `mix(color, color3d, tilt_morph)`. With one layer there is no such mix:
    **scale the ts screen-space compositing by (1-T) explicitly** (e.g. `ts_op *= (1.0 -
    tilt_morph)`), or texture shading double-applies at tilt (once as occ, once as
    multiply).
  - Keep Phase 1's `raw2d`/`shading` computation in the normal block unchanged — it
    still feeds `contour_color = shading*contour_color` (contour tint stays
    tilt-independent by design) and the raster-basemap branch. The unified function
    recomputes the T=0 luma in the color block; the tiny duplication is fine.
  - Composite `base_color` (contours/hypsometric) over the layer ONCE, at the end
    (both old layers did it separately; same result, single code path).
  - Unify on ONE sun vector: rename Phase 1's `s2_cam`/`s2_world` to `s_cam`/`s_world`,
    move the world-elevation clamp [20°,70°] (currently in the 3D block) into the normal
    block so it applies at all T (no-op at zenith: elevation is 35.26°), and delete the
    old 3D-only `s_cam = normalize(vec3(-0.5, 0.5, 0.707))` — the sun at tilt is now
    35.26° instead of 45°, deliberate (raking light models better; tune by eye later).
    Note: in plain-2D-tilted views (terrain off, camera tilted) the clamp slightly
    changes Phase-1 behavior — harmless improvement, don't chase parity there.
- **Keep as-is**: the ts tint/multiply screen-space compositing fading with T and `occ`
  living on at all T (already right); haze contrast attenuation
  (`L = mix(L, 1.0, u_haze_contrast*haze)`) — apply after the shoulder; contours/
  hypsometric via `base_color` (tilt-independent, composited after, unchanged).
- **Delete**: the entire separate `color3d` block; the `sh`/0.95-softclip lines; the
  fixed-theoretical-max normalization and its long comment (the flat-ground normalization
  + shoulder subsumes the white-patch protection it provided — verify at Dolomites, see
  acceptance).
- **Sliders** (F8 pattern): keep `u_sky_weight`, `u_sun_weight` (new meanings/defaults),
  add `u_amb_weight_3d`, `u_sun_wrap`, `u_sun_gamma`, `u_shoulder_start`,
  `u_shoulder_max`; keep `u_occ_strength`, `u_occ_min`, `u_shade3d_alpha`,
  `u_shade3d_gain`, `u_haze_contrast`. Update the comment block above the sliders.

**Pitfalls**
- The old 3D block reads `ts_shade` computed in the *color* block — `terrainLuma` must be
  called where `ts_shade`, `haze`, `normal` are all in scope (the color block, as today).
  The `occ` computation (`clamp(1 + u_occ_strength*(2*ts_shade-1), u_occ_min, 1.15)`,
  ~line 550) and its `#else const float occ = 1.0` fallback stay.
- `sun_flat` uses the **clamped** `s_world.z`. Keep the clamp-then-renormalize of
  `s_world` exactly as the existing code does (~lines 560-563) — it preserves azimuth.
- Don't let the shoulder run on the raster-basemap (`ELEVATION_INDEX`) branch
  unless also verified there — scope this phase to the blend-over path (like the old
  plan did), keeping Phase 1's explicit-formula raster branch as-is.
- GLSL: no non-constant `mix` on `#define`s; all tilt interpolation on uniforms/locals.
  `tilt_morph` is currently computed in the `normal` block under
  `TANGRAM_TERRAIN_3D && HILLSHADE_BLEND_OVER` (~line 425); in plain 2D builds define
  `const float tilt_morph = 0.0;` so `terrainLuma` compiles unguarded (today's code
  compiles the whole 3D section out — the unified function must exist in both builds).

**Acceptance**
- T=0 parity: rotation-0 zenith shots diff-clean vs. Phase 1 output (per the
  normalization decision above; document the measured diff).
- Flat-gray fixed: at Howe/Lions tilt 45-60°, faces of the same ridge with different
  aspects must be visibly separated. Quantify: pick 2 opposing-aspect slope crops per
  view; their mean-luminance difference must be ≥3× the old baseline's. Local-contrast
  proxy (Laplacian mean, old plan Phase B methodology) at 60° ≥ the zenith value.
- No white clipping at Dolomites tilt 45° (histogram: <0.5% of terrain pixels at 255 in
  all channels within a cliff crop — the shoulder must hold what the old fixed-max
  normalization was protecting).
- Full rotation matrix re-run: no inversion, no discontinuity (old Phase C method).

### Phase 2 — Revision after first user test (2026-07-10)

First build of Phase 2 still "flattened everything at tilt" (user report). Diagnosis
found THREE mechanisms, the first of which was also the true cause of the original
pre-v2 flatness — the sky-vs-sun weight question (F5.1) was largely a red herring:

1. **The `gain·L − (gain−1)` compositing is a WINDOW, and it was crushing everything.**
   Gain g maps only `L ∈ [1 − 1/g, 1]` onto visible grays; all L below the floor clamps
   to identical black (then composites as a uniform darkening at the layer's alpha =
   uniform gray mountains). Gain 4.5 → floor 0.78. The 2D formula co-evolved with its
   0.7 ambient floor (L ∈ [0.7, 1.1] by construction — inside the window); any honest
   3D model produces L ≈ 0.3–0.6 over most sloped terrain — ALL of it crushed to the
   floor, in the old sky-dominant model and the new sun-dominant one alike. **Fix:**
   `u_shade3d_gain` default 4.5 → **1.4** (window floor ≈ 0.29 ≈ w_amb/norm, so the
   whole L range grades instead of clipping), `u_shade3d_alpha` 0.35 → **0.55** (the
   wider window carries less punch per unit alpha). Worked example at T=1, 30°-true
   slopes at 2× lighting exaggeration: sun-facing L≈1.09→rgb 0.98 vs. sun-averted
   L≈0.55→rgb 0.37 — Δ≈0.6 in the overlay, ≈0.34 on the map at α 0.55; flat ground
   rgb≈0.97 (nearly untouched, as designed).
2. **Tilting RAISED the sun.** The screen-anchored `s_cam=(-1,1,1)` sits at 35.26°
   world elevation at zenith but ~50–55° by 45–60° tilt — inside the [20°,70°] clamp,
   so the clamp never engaged; raking light silently became near-overhead light.
   **Fix:** hard-SET the world elevation to `u_sun_elev` (default 35.264° =
   asin(1/√3), exact zenith no-op → parity preserved), keeping only the azimuth
   screen-anchored. Replaces the clamp entirely (normal block).
3. **Normals were lit at 4.5× exaggeration on 1×-displayed geometry.** At 4.5×, every
   real mountain slope's normal is near-horizontal (n.z ≈ 0.1–0.4): the sky term
   saturates to a constant across all steep terrain. **Fix:** `hscale =
   mix(u_exaggerate, u_exaggerate_3d, tilt_morph)`, new `u_exaggerate_3d` default 2.0
   (slider). Required moving the `tilt_morph` definition ahead of the normal
   computation; its guard is now `defined(TANGRAM_TERRAIN_3D) && !ELEVATION_INDEX`
   (equivalent to the HILLSHADE_BLEND_OVER check, which isn't #defined yet at that
   point in the normal block).

Implication for Phase 4 (cast shadows): with gain ~1.4 the overlay is nearly a direct
luminance layer, so shadow terms will read almost literally — good; no window math to
fight. Implication for tuning: `u_lum_cap` is now mostly moot (the shoulder does the
work); leave it.

### Third revision: yaw-only sun anchoring (2026-07-11)

Diagnosis of the second revision's own fix (elevation hard-set, item 2 above): hard-
setting elevation but still rotating the sun by the **full** camera rotation
(`s_world = s_cam * mat3(u_view)`) let camera **pitch** leak into the sun's world
**azimuth** — facing north, azimuth drifted NW (zenith) → due W (45° tilt) → WSW (60°
tilt), ~65° of drift across the tilt range, continuously relighting the terrain while
tilting (reported as a "weirdly lustrous texture"). Root cause: `mat3(u_view)` is a
full 3D rotation, so pitch rotates the sun vector just as much as yaw does; only yaw
should move a screen-anchored sun.

**Fix**: anchor the sun's azimuth to the map's **yaw only** — never pitch. Camera-right
in world space is row 0 of `u_view` (world→camera; rows are the camera basis vectors in
world space); under pure yaw+pitch (the map never rolls) camera-right stays horizontal,
so its xy component *is* the yaw, uncontaminated by pitch. Project that onto the map
plane to get screen-right (`e`) and screen-up (`h` = `e` rotated 90° CCW), then place the
sun at `u_sun_azimuth` degrees CCW from `e` at fixed elevation `u_sun_elev`. New uniform
`u_sun_azimuth` (default 135°, GUI slider 90–180 step 5) replaces the fixed
`normalize(-1,1,1)` camera-space vector.

**Deliberate tradeoff** (Sebastian's choice): tilting the camera now never moves the sun
at all — shading polarity at every tilt is identical to the 2D zenith polarity. Facing
north, this means the predominantly-visible lit sides stay the same (south-facing slopes
shaded) as tilt increases, rather than the sun's world position "correctly" changing with
viewing angle. This is accepted in exchange for eliminating the tilt-relighting artifact;
2D↔3D continuity wins over strict world-space realism.

**Zenith parity is exact by construction**: at north-up zenith, `cam_right` = world east
= `(1,0,0)`, so `e=(1,0)`, `h=(0,1)`. With `u_sun_azimuth=135°`, `az_dir = (cos135°,
sin135°) = (-0.70711, 0.70711)`. With `u_sun_elev=35.264°` (`asin(1/√3)`),
`cos(35.264°)=√(2/3)=0.81650`, `sin(35.264°)=√(1/3)=0.57735`. `s_world = (az_dir *
0.81650, 0.57735) = (-0.57735, 0.57735, 0.57735) = normalize(-1,1,1)` exactly — bit-
identical to every prior revision's T=0 output.

### Second revision: texture shading invisible at tilt (2026-07-10)

Second user-test finding: texture shading itself, not just the base hillshade, goes
dark at tilt. Diagnosis: the 2D whole-color ts multiply/tint (`ts_op = u_texture_
shading_opacity * (1.0 - tilt_morph)`) deliberately fades to zero as tilt increases,
by design (F5/Phase 2 pitfalls) — leaving texture shading to reach the tilted view only
through `occ`, which multiplies just the sky slice of the light (`w_sky·sky ≈ 0.30·sky`),
is ceiling-clamped at a hardcoded 1.15, and is haze-attenuated twice. Net ridge/valley
luminance swing ≈0.08 at full tilt vs. ≈0.4–0.5 in 2D — roughly 5–6× too weak to see.
**Fix**, three new sliders, all tilt_morph-gated (exactly inert at T=0): (1)
`u_texture_shading_opacity_3d` (default 0.5) — a tilt-side whole-color ts opacity that
ramps IN as the 2D one ramps out, with its own haze falloff; (2) `u_occ_ambient`
(default 0.5) — lets occ also darken the ambient term (ambient is sky light too),
raising occ's modulated share of the light from ~0.30·sky toward ~0.55+; (3)
`u_occ_max` (default 1.25) — replaces the hardcoded 1.15 ceiling, which was silently
halving the ridge-brightening half of the signal (unclamped ridge value reaches ~1.32
at strength 0.8).

### Phase 3 — Chromatic shading (warm light / cool shadow)

Imhof and swisstopo separate faces by **hue temperature**, not just luminance: shadowed
sides cool blue-violet (sky-lit), sunlit sides warm. Games do the same (sky-fill color).
This buys face separation without more luminance contrast.

**Change** (hillshade.yaml, blend-over path only):
```glsl
// t_warm in [0,1]: sun-lit fraction of the fragment's light, 0.5 = neutral
float t_warm = 0.5;
#if 3D-or-always — see decision below
t_warm = clamp(w_sun_eff * sun * sunvis / max(L_pre_shoulder, 1e-3) /
               (w_sun_eff * sun_flat) * 0.5 + ...  // OR simply: t_warm from `sun` alone:
t_warm = sun * sunvis;                              // simplest, recommended first cut
#endif
vec3 tint = mix(u_shade_cool.rgb, u_shade_warm.rgb, t_warm);  // colors ~ (0.88,0.92,1.06)/(1.06,1.0,0.90) around 1
vec3 rgb  = (gain*L - (gain-1.0)) * mix(vec3(1.0), 2.0*tint - 1.0 + ... , u_shade_warmth);
```
Implementation note for the agent: define the two tints as **multipliers around 1.0**
(e.g. cool `(0.90, 0.95, 1.08)`, warm `(1.08, 1.02, 0.90)`) stored in color uniforms
scaled at use (`tint*2-0.5`-style remaps invite bugs — instead store them directly as
`vec3` uniforms with those literal defaults and `type: color` sliders around 0.5-centered
values only if the color picker requires [0,1]; otherwise plain 3-float uniforms and 3
scalar sliders for warmth/coolness/hue are fine). `u_shade_warmth` (0 = today's exact
gray, default ~0.35 at T=1) scales the effect; interpolate
`warmth = mix(u_shade_warmth_2d, u_shade_warmth, T)` with `u_shade_warmth_2d` default
**0** so the 2D look is untouched until Sebastian opts in (swisstopo's own relief has a
subtle blue-gray cast — he may want ~0.15 in 2D; that's a slider decision, not a code
decision).

**Pitfalls**
- The overlay is alpha-composited over colored landcover: a tint multiplier <1 on some
  channel can push `gain*L-(gain-1)` further negative → clamps to 0 → over-darkening in
  that channel. Clamp the tinted rgb at 0 explicitly and eyeball forests (dark green
  base) for hue shifts.
- Keep the tint OFF the contour/hypsometric `base_color` composite (contours stay ink-
  colored).
- Apply the same `mix(·, 1.0, u_haze_contrast*haze)` attenuation to the tint deviation
  (distant terrain drifts to neutral haze — aerial perspective desaturates).

**Acceptance**: A/B at Lions 45°/60° tilt with warmth 0 vs 0.35 — same luminance
histogram (warmth must not change overall brightness: verify mean |ΔY| < 2/255), visibly
warmer sun-facing / cooler shadowed faces. Zenith unchanged (warmth_2d=0).

**Results (2026-07-11, implemented)**: Landed as specified in `hillshade.yaml`'s color
block, immediately after `color = vec4(vec3(gain*L - (gain-1.0)), alpha)` and before the
`base_color` composite (contours/hypsometric stay untinted, as required). `t_warm = sun *
sunvis` — this is also the free Phase 3/4 interplay: cast-shadowed fragments have
`sunvis` near 0, so they automatically read cool regardless of raw sun-facing angle.
Defaults landed as given: `u_shade_cool: [0.88, 0.94, 1.10]`, `u_shade_warm: [1.10, 1.03,
0.88]` (plain vec3 uniforms, yaml list syntax, no slider - gui_variables only binds
scalars/colors), `u_shade_warmth: 0.5`, `u_shade_warmth_2d: 0.0`, with GUI sliders "3D
Shade Warmth" / "2D Shade Warmth" (0-1, step 0.05). Haze attenuates the warmth strength
itself (`warmth *= 1.0 - u_haze_contrast*haze*tilt_morph`), matching the pitfall note
above. `color.rgb` is explicitly `max(...,0.0)`-clamped after the tint multiply, per the
implementation spec, to stop a <1 tint channel from flipping a negative `gain*L-(gain-1)`
value into a brighter (post-clamp) one instead of a darker one.

Zenith parity note (flagged, not silently fixed): at T=0, `warmth =
mix(u_shade_warmth_2d=0, u_shade_warmth, 0) = 0.0` exactly, so
`mix(vec3(1.0), tint, 0.0) = vec3(1.0)` bit-exact and the tint multiply is a true no-op.
However the added `max(color.rgb, 0.0)` is unconditional, and the pre-existing 2D formula
can itself produce slightly negative `color.rgb` at T=0 (e.g. `raw2d` bottoms out at 0.7
on a near-vertical face angled away from both light terms, giving `4*0.7-3 = -0.2`) which
used to flow unclamped into the translucent blend-over. Those rare pixels now get an
extra clamp-to-0 that didn't exist before Phase 3, a technically non-bit-exact zenith
change; it was kept because the spec calls for it and any real-world instance is confined
to knife-edge cliff faces where the previous negative excursion was already deep into
extreme-shadow territory. User visual verification of the warm/cool split is pending.

### Phase 4 — Cast shadows (mip-accelerated horizon march)

The flagship. Terrain-only shadow test along the sun direction using the elevation
mosaic's real mip pyramid (F6). Sun term becomes `sun * sunvis`.

**Where**: new function in the hillshade `global` block (next to `textureShading()`),
called from the color block; everything `#ifdef ELEVATION_MOSAIC` +
runtime-skipped when `u_shadow_strength < 0.005` (uniform-coherent branch, ~free).

```glsl
float sunVisibility(vec2 uv, vec3 s_world, float h0, vec2 texwh, vec2 dxy_elev, float coslat) {
    // horizontal sun direction in tile-UV space. World: +x east, +y north.
    // PITFALL (verify empirically once, see below): raster v may run north->south;
    // if so flip sdir.y. Get it right by testing, not by assumption.
    vec2 sdir = normalize(s_world.xy);
    vec2 sdir_uv = sdir / (dxy_elev * texwh);          // uv units per projected meter... 
    // ^ dxy_elev = proj meters per texel; texel per uv = texwh; so proj meters per uv unit
    //   = dxy_elev * texwh (per axis). March parameter d below is in PROJECTED METERS.
    sdir_uv = sdir / (dxy_elev * texwh);
    float tanSun = s_world.z / max(length(s_world.xy), 1e-4) / coslat;
    // ^ heights are TRUE meters; horizontal distances in projected meters; true horiz
    //   = proj * coslat  =>  tan in (true m / true m) needs the /coslat on the proj run.
    //   DOUBLE-CHECK this factor against how `grad` handles coslat in the normal block.
    float maxTan = -1e9;
    float d = u_shadow_d0 * dxy_elev.x;                // start ~1.5 texels out
    float maxD = <largest d with uv+sdir_uv*d still in [-0.95, 1.95] per axis>;
    for (int i = 0; i < SHADOW_STEPS; i++) {           // SHADOW_STEPS 16, compile-time
        if (d > maxD) break;
        float lod = clamp(log2(d / dxy_elev.x) - 2.0, 0.0, 99.0); // widen w/ distance;
                                                       // getElevationAtLod re-caps internally
        float h = getElevationAtLod(uv + sdir_uv * d, lod);
        maxTan = max(maxTan, (h - h0 - u_shadow_bias * d) / d);
        d *= 1.45;                                     // geometric: 16 steps cover ~256 texels
    }
    // soft comparison: penumbra width grows with the gap
    float vis = clamp((tanSun - maxTan) / max(u_shadow_soft, 1e-3) + 1.0, 0.0, 1.0);
    return 1.0 - u_shadow_strength * (1.0 - vis);
}
```
(The agent should treat the above as a spec, not paste-ready code — get the
meters/uv/coslat algebra consistent with the `normal` block's existing conventions and
write it with comments in the yaml style.)

**Integration**
- `sunvis` multiplies **only the sun term** in `terrainLuma` (never sky/occ).
- Attenuate with haze: `sunvis = mix(sunvis, 1.0, u_haze_contrast * haze)`.
- Strength interpolates with tilt: `u_shadow_strength_eff = mix(u_shadow_strength_2d,
  u_shadow_strength, T)`, defaults `0.0 / 0.6`. (Swisstopo uses no cast shadows at
  zenith; leaving the 2D slider available lets Sebastian try a whisper of it.)
- Sliders: `u_shadow_strength`, `u_shadow_strength_2d`, `u_shadow_soft` (~0.15),
  `u_shadow_bias` (~0.01, self-shadow acne guard), `u_shadow_d0` (~1.5).

**Pitfalls (all real — check each)**
- **UV y orientation**: web-Mercator tile rows run north→south; whether tile-local v does
  depends on tangram's raster UV convention. Verify empirically: force
  `s_world = normalize(vec3(0,1,0.3))` (due north, low), render a known isolated peak
  (the Lions), check the shadow falls SOUTH of the peak on screen (north-up view). If it
  falls north, flip `sdir_uv.y`. One shot, definitive. Do this FIRST.
- **Reach cap** (F6): the mosaic ends ±1 tile out. At z12 a tile ≈ 10 km — plenty; at z15+
  long shadows from big peaks would need >1 tile and get truncated. Mitigate: fade the
  shadow's contribution as `d → maxD` (multiply `(1 - smoothstep(0.7,1.0,d/maxD))` into
  the `maxTan` candidate's weight, or simply accept truncation — truncated shadows fail
  *soft*, they just end early). Document; don't chase.
- **Tile seams**: neighboring tiles' mosaics share only 2 of 3 rings, so their marches can
  see different data near the reach cap → the same class of seam texture shading had.
  The distance-fade above is the mitigation; check seams explicitly in flats at tile
  boundaries (the Howe Sound fjord water + valley floors show them best).
- **Acne**: h0 is the interpolated `elev` (F6), the march samples mip'd data — on steep
  slopes early samples can poke above the ray. `u_shadow_bias·d` plus starting at
  ~1.5 texels handles it; verify on the Dolomites cliffs (worst case).
- **Mosaic-transient tiles**: proxy tiles can briefly carry a plain (non-mosaic) texture
  while `ELEVATION_MOSAIC` is compiled in (see terrain-illumination-plan.md "white
  patches" §1 — `getExistingMosaic` made this rare, not impossible). The march would read
  garbage UVs on such tiles… but so does `textureShading()` already, and it's accepted as
  a brief transient. Same acceptance here; no new handling.
- **Cost**: 16 `textureLod` taps on top of ~9+8 existing. Desktop-fine (this app targets
  desktop + mobile; `LIBGL_ALWAYS_SOFTWARE` test runs will be slower — raise `WAIT`).
  The `u_shadow_strength==0` branch skips the loop entirely; on mobile the default can
  ship 0 until profiled. No C++ needed.

**Acceptance**
- North-shadow orientation test (above) passes.
- Lions 45° tilt, rotation sweep {0,90,180,270}: shadows stay on the screen-lower-right
  of peaks (sun is screen-upper-left) — consistent with the shading, no popping between
  rotations (shadow direction rotates smoothly with the screen-anchored sun).
- Dolomites cliffs: no acne speckle at strength 0.6.
- Perf: frame time in the live app (Release build, `--terrain_3d.enabled true`, Lions
  45°) within 15% of strength-0. If worse, drop SHADOW_STEPS to 12 before optimizing
  anything else.
- Seam check as above.

**Results (2026-07-11, implemented)**: Landed as `sunShadow()` in the hillshade style's
`global` block (inside the same `#if defined(ELEVATION_MOSAIC) && defined
(TANGRAM_FRAGMENT_SHADER)` region as `textureShading()`, reusing its
`getElevationAtLod` forward declaration), called from the color block in place of the old
`sunvis = 1.0` placeholder. Algebra was corrected relative to this section's original
sketch, per convention (a) above: `tanSun = s_world.z / length(s_world.xy)` (no `/coslat`
— `s_world` is a pure unit direction, so its own xy/z ratio is already the true tangent),
and the horizon side is `tanH = (h - h0) / (d * coslat) - u_shadow_bias` (`d` in
projected meters, `d*coslat` converts to the true-meter run to match `h`/`h0` which are
true meters). Defaults landed as given: `u_shadow_strength: 0.6`,
`u_shadow_strength_2d: 0.0`, `u_shadow_soft: 0.15`, `u_shadow_bias: 0.01`,
`u_shadow_d0: 1.5`, `TERRAIN_SHADOW_STEPS: 16` (compile-time loop bound + `break`, same
pattern as `TEXTURE_SHADING_MAX_LEVELS`). Sliders: "3D/2D Shadow Strength" (0-1, 0.05),
"Shadow Softness" (0.01-0.5, 0.01), "Shadow Bias" (0-0.05, 0.005), "Shadow Start
(texels)" (0.5-4, 0.25), grouped with a comment noting the feature requires the Texture
Shading checkbox (ELEVATION_MOSAIC) on.

**uv-north justification (documented, not yet empirically re-verified this session)**:
tile-local uv's v-axis is assumed to equal world north, per the existing code's own
convention rather than a fresh assumption — the `normal` block already computes
`normal.xy = -hscale*grad/coslat` from `grad = vec2(h21-h01, h12-h10)/2` (i.e. +u and +v
central differences) and dots that world-space-oriented normal against `s_world` for all
existing 2D/3D lighting, which has rendered with correct polarity (ridges lit, valleys
shaded, consistently oriented) through every prior revision. That would not be possible
if +v corresponded to world south instead of north, since the sign of the y-term in every
dot product would be flipped. `sunShadow()` reuses `sdir_uv = sdir/(dxy_elev*texwh)`
without any extra sign flip, consistent with that convention.

**Pending — user visual verification required** (per the task's own acceptance criteria,
not run this session: no build/screenshot was taken). At north-up, shadows must fall
*opposite* the sun, i.e. toward screen-lower-right (sun is screen-upper-left,
azimuth 135°). **If shadows instead fall toward the wrong side (screen-upper-left, same
side as the sun), the fix is to negate `sdir_uv.y` inside `sunShadow()` — do not add a
uniform for this; it is a fixed sign convention, not a tunable.**

Phase 3/4 interplay confirmed by construction: `t_warm = sun * sunvis` in the Phase 3
code means cast-shadowed fragments (`sunvis` pulled toward 0 by `sunShadow()`)
automatically drift toward the cool tint even where the raw sun-facing dot product
alone would read warm — shadow and color temperature reinforce each other for free,
no extra wiring needed.

### Phase 3/4 debugging (2026-07-11): "no cast shadows or color separation, the new controls do nothing"

User report after landing Phase 3+4: no visible cast shadows, no warm/cool separation,
new sliders appear inert. Investigated by code reasoning only (no build/run/screenshot
this session, per constraint) plus headless-instrumentation measurements from a prior
session, reasoned about below. Three lines of investigation, in the order run:

**A. Why might `u_exaggerate_3d` (or another new uniform) read as 0 in the executed
shader?** Instrumentation from a prior session painted `tilt_morph` (≈1.0, correct),
`shadow_strength`/`u_shadow_strength` (≈0.6, correct), `1.0 - sunShadow(...)` (crisp,
plausible shadows, correct march), and `sun` (the half-Lambert sun term) — the last
came back **nearly constant** across a whole Howe Sound/Lions frame (mean ≈0.58, p10
≈0.51, p90 ≈0.62), matching `sun_flat` for the 35.264° sun almost exactly. Since
`sun_flat` is the value `sun` takes on perfectly flat ground, a `sun` that never departs
from it over extreme relief means the effective lighting normal is flat everywhere,
i.e. `hscale = mix(u_exaggerate, u_exaggerate_3d, tilt_morph)` ≈ 0 at `tilt_morph` ≈ 1,
i.e. `u_exaggerate_3d` reads ≈0 in-shader despite the yaml default of 2.0.

Candidates examined and eliminated, with evidence:
- **YAML indentation/placement**: `u_exaggerate_3d: 2.0` (hillshade.yaml, uniforms
  block) sits at the identical indent as `u_sun_elev`/`u_sun_azimuth`, siblings that
  demonstrably arrive (confirmed by `sun_flat` matching 35.264° exactly, and shadows
  landing on the correct side per the azimuth). Byte-checked with `cat -A`; no tabs, no
  stray whitespace.
- **`elev > 0.` gate zeroing `hscale`** (hillshade.yaml normal block, `hscale = elev > 0.
  ? mix(...) : 0.`): ruled out — the cast-shadow march (`sunShadow()`) reads real,
  varying elevation differences along the ray to produce its crisp, correctly-oriented
  shadow pattern, so `elev` is provably not stuck at/below 0 over this terrain. This
  ternary is also unchanged from the pre-Phase-2 code (same pattern, just `u_exaggerate`
  before Phase 2 added the `mix`), so it isn't a new regression.
  candidate, verified via `git log -p`.
- **Vertex-vs-fragment uniform location caching** (the task's lead hypothesis, per the
  `-1`-caching comment in `tangram-es/core/src/style/style.cpp` around
  `setupTileShaderUniforms`): eliminated. `#pragma tangram: normal` exists in BOTH
  `tangram-es/core/shaders/polygon.vs:146` and `polygon.fs:85`, but the vertex copy is
  gated `#if defined(TANGRAM_LIGHTING_VERTEX)` (`polygon.vs:143`); hillshade never sets
  `lighting: vertex` (default is `LightingType::fragment`, `style.h:143`,
  confirmed by `sceneLoader.cpp:1044-1051` — no `lighting:` key means the constructor
  default holds), so the vertex-side normal block is compiled out entirely. The
  `u_exaggerate_3d` reference lives only in the fragment shader, exactly like
  `u_sun_elev` (which arrives fine) — no stage asymmetry exists between them.
- **Uniform-plumbing code path itself** (`sceneLoader.cpp:1244-1266` `parseStyleUniforms`
  / `loadShaderConfig`, `style.cpp:163-201` `setupSceneShaderUniforms`): `u_exaggerate_3d`
  is parsed and bound through the exact same generic loop as every sibling float
  uniform (declared once via `addSourceBlock("uniforms", ...)`, pushed once via
  `styleUniforms().emplace_back(name, value)`, applied once per frame via
  `setUniformf`). No special-casing exists anywhere in this path for any specific
  uniform name; grepped for duplicate declarations of `u_exaggerate_3d` across all of
  `assets/scenes/*.yaml` — only one.
- **Shared-shader-program dedup** (`style.cpp:120-132`: a style reuses another style's
  compiled `ShaderProgram` if their full vertex+fragment source text matches exactly,
  which — combined with `UniformLocation`'s cache being tied to the specific
  `ShaderProgram` instance's `m_glProgram`, `shaderProgram.h:34-46` — would be a real
  cross-contamination risk if it fired): eliminated for this case. Hillshade's
  fragment source is unique (its own `normal`/`color`/`global` blocks, texture-shading
  functions, `sunShadow()`); no other style in the mixed scene (`terrain-ground`,
  `heightglow`, `unlit-*`) generates byte-identical source, so hillshade's
  `m_shaderProgram` is never shared.
- **Saved-override persistence** (Task B mechanism, see below): checked the actual
  user config (`~/.config/Ascend/mapsources.yaml`, `~/.config/Ascend/config.yaml`).
  Neither contains ANY override for `u_exaggerate_3d` or any other hillshade shading
  uniform (`stylus-bike-hike`'s only saved updates are `global.show_bike`/
  `global.show_trails`; `terrain_3d.updates` in config.yaml is `{}`). `u_exaggerate_3d`
  is also a brand-new uniform introduced in this same work, so no pre-existing saved
  slider value could exist for it regardless. This mechanism does not explain the
  current symptom, though it remains a real latent risk (documented in Task B below).

**Conclusion for A**: every mechanism this session could name and check by reasoning
was eliminated with concrete file:line evidence. The measurement (`sun` ≈ `sun_flat`,
constant) is internally consistent with `u_exaggerate_3d` reading ≈0 in-shader, but no
C++ code path was found that treats this uniform differently from siblings that
demonstrably work — nothing here explains why ONE simple float uniform, bound through
the exact same generic loop as its neighbors, would silently fail to arrive. **This is
reported honestly as unresolved by pure code reasoning; it needs a runtime check** (a
debug build that logs `glGetUniformLocation` results per style uniform name, or an
on-screen readout of `u_exaggerate_3d` alongside `u_sun_elev` at the same tilt) that is
out of scope for this session's no-build constraint. If the next session can build: add
a one-line `LOGE` in `Style::setupSceneShaderUniforms` printing name + resolved GL
location + value for every style uniform, once, and diff hillshade's dump against a
known-good uniform.

**B. The user-side override layer** (mechanism, confirmed real; not the live cause —
see the elimination note above). Slider drags in the Sources panel
(`app/src/mapsources.cpp:627-656` `processUniformVar`) mutate the in-memory uniform
value immediately (`uniform.second.set<float>(val)`) and stage the change in
`app->sceneUpdates` / `currUpdates` (session-only). Nothing is written to disk until
the user clicks **"Save Source"** (the disk icon at the top of the source's edit panel,
`saveBtn`, `mapsources.cpp:1060-1065`), which calls `createSource(currSource)`
(`mapsources.cpp:318-359`): it copies `currUpdates` into
`mapSources[<source-key>]["updates"]` and calls `saveSources()`
(`mapsources.cpp:208-220`), which writes the **entire** in-memory source list to
`~/.config/Ascend/mapsources.yaml` (path is `sources.file` in `config.yaml`, default
`mapsources.yaml`, resolved relative to the app's config directory —
`mapsources.cpp:173-180`). On every subsequent app start (or any `rebuildSource()`,
e.g. switching sources), `mapSources[<key>]["updates"]` is read back
(`mapsources.cpp:269-270` inside `rebuildSource`) and layered into `app->sceneUpdates`,
which the scene loader applies **on top of** the yaml's own defaults — i.e. any uniform
path saved this way is pinned indefinitely, silently masking every future default
change shipped for that uniform, until removed. A second, scene-wide (not per-source)
override layer exists at `terrain_3d.updates` in `config.yaml` (applied in
`rebuildSource`, `mapsources.cpp:281-284`, whenever 3D terrain is on).

**Live-config check (this session)**: read the actual files. `stylus-bike-hike`
(`last_source` in `config.yaml`) has `updates: {global.show_bike: true,
global.show_trails: true}` only — no hillshade shader-uniform overrides.
`terrain_3d.updates: {}` in `config.yaml`. No source in `mapsources.yaml` pins any
`styles.hillshade.shaders.uniforms.*` path. **So this mechanism is not currently
masking anything** — but it is a real, easy-to-trigger trap for future sessions of
hand-tuning, so documenting the discard procedure below is worthwhile regardless.

**User instructions — inspecting/clearing saved overrides:**
1. Open `~/.config/Ascend/mapsources.yaml` in a text editor.
2. Find the entry for whichever source you use for hillshading (currently
   `stylus-bike-hike`, per `config.yaml`'s `sources.last_source`) and any other source
   whose `layers:` includes `hillshade`/`stylus-osm-terrain`.
3. Look under that entry's `updates:` key for anything starting with
   `styles.hillshade.shaders.uniforms.` — each such line is a pinned override that will
   beat the yaml default on every load. Delete the line to fall back to the shipped
   default (or edit the value directly).
4. Also check `~/.config/Ascend/config.yaml`'s `terrain_3d: { updates: {...} }` — same
   mechanism, applies whenever 3D terrain is enabled regardless of source.
5. There is no in-app "reset to default" button for an individual slider; the only
   in-app control is to NOT press "Save Source" after tuning (session-only changes
   vanish on restart/source switch), or to edit the yaml files directly as above while
   the app is closed.

**C. Robustness fixes applied regardless of A/B** (hillshade.yaml, color block):
1. **Direct-luminance shadow darkening** ("Schattenton"): cast shadows previously only
   multiplied the sun term (`sunvis`) — but the sun is deliberately backlit at every
   tilt (yaw-only screen anchoring, third revision), so most shadowed pixels already
   have a small `sun` value and gain little extra visible darkening from `sunvis`
   alone. Added `shadow_amt` (the same haze-attenuated shadow signal, exposed outside
   the `#ifdef ELEVATION_MOSAIC` block) and `L *= mix(1.0, 1.0 - u_shadow_darken,
   shadow_amt * tilt_morph)` right after the haze-contrast attenuation line, alongside
   (not replacing) the existing sun-term multiply. New uniform `u_shadow_darken`
   (default 0.25, slider "Shadow Darken (direct)", 0-1 step 0.05). Exact no-op when
   `u_shadow_darken == 0` (the `1.0 - u_shadow_darken` factor) and exact no-op at T=0
   (the explicit `* tilt_morph` factor, redundant with but independent of
   `shadow_amt`'s own zeroing, matching this file's belt-and-suspenders zenith-parity
   style elsewhere).
2. **Warm/cool tint too weak to see.** Verified the compositing arithmetic: tint
   deviation (±0.10-0.12) × `u_shade_warmth` (0.5) × haze damp (~0.7-1.0) × overlay rgb
   range (~0.7) × translucent alpha (0.55) multiplies out to roughly 1-4/255 of actual
   on-screen chroma difference — invisible even though the shader logic was correct.
   Doubled the tint deviations (`u_shade_cool: [0.88,0.94,1.10] → [0.78,0.88,1.24]`,
   `u_shade_warm: [1.10,1.03,0.88] → [1.24,1.06,0.78]`) and raised `u_shade_warmth`'s
   default `0.5 → 1.0`; `u_shade_warmth_2d` stays 0 (2D look unchanged). Added a
   comment at the uniform defaults noting the alpha-compositing bottleneck for future
   tuning sessions.

Neither C fix depends on A's root cause being found or B being the live cause — both
are applied unconditionally as improvements to the existing feature, and both remain
exact no-ops at T=0 / default-off per the zenith-parity contract.

### Phase 5 — FUTURE (not now): realistic sun mode

Ephemeris-driven sun (NOAA solar position from date/time/lat-lng — ~30 lines of C++ in
MapsApp, uniform `u_sun_world_override` + mode flag), time-of-day slider UI, 3D-tilt only,
requires Phase 4 (a realistic sun without its shadows is pointless), auto-reverts to
screen-anchored in 2D (a south sun at north-up zenith inverts relief — hard constraint,
see terrain-illumination-plan.md §2 research notes). Sketch only; do not build until the
screen-anchored default is tuned and Sebastian asks.

---

## Part III — Order, dependencies, discipline

- Phases strictly in order 0→4; each lands as its own commit pair (submodule commit only
  if C++ changes — none expected; outer-repo commit for yaml/docs) on
  `texture-shading-phase4-integration` with the acceptance evidence noted in the commit
  message. Working tree was clean at plan time (last commits: outer `51a2c0b`, submodule
  `bbbedbe8`).
- After each phase: `make -f tests.mk` (baseline 1763 assertions / 170 cases), full app
  build, and the phase's shot matrix reviewed as a montage before moving on.
- Keep every magic number on a slider (F8). Sebastian tunes by hand; the defaults above
  are starting points, not decisions.
- Update THIS file's tail with a Results section per phase (same convention as
  terrain-illumination-plan.md).
