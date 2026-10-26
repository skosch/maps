# Tree / forest rendering plan (design + research, nothing integrated)

Status: 2026-10-24. Design only. No scene YAML or C++ was changed. Prototypes (GLSL, compile-checked with
`glslangValidator -S frag`, never run on a GPU, never looked at) are in
`/tmp/claude-1000/-home-sebastian-projects-maps/d2a9956e-3c96-433a-95f1-25135c060ba4/scratchpad/trees/`
(`trees_frag.glsl`, `canopy_frag.glsl`). **All ms/ALU numbers below are paper estimates**; the repo has no
measurement of a tree shader yet, and the newest trace (`assets/profiles/trace-20261024-155821.json`, z11 tilt 0)
shows `style-gpu:unlit-polygons` already at ~70 ms/frame avg and `hillshade` ~50 ms on whatever machine it was taken
on (the memory notes say "needs clean-machine profile"), so the absolute baseline is itself unreliable.
Assumptions I had to make (no questions possible) are marked **[A]**.

---

## 0. Recommendation

**Build "cell trees" in the fragment shader of the existing forest polygon, with an analytic footprint LOD that
dissolves into a painterly canopy, and keep it a YAML-only feature for the first four steps.**

In one paragraph: forests stay land-cover polygons (draped, tessellated, fogged, hillshade-veiled, snow-capable, contoured,
exactly as now). A new style `forest-trees` (mix of `unlit-polygons`) paints, per pixel, a world-anchored jittered
(hex-staggered) grid of tree *sprites*: each tree is a 2D signed-distance shape (spire with tiers, or cauliflower blob)
in **screen pixels**, standing on its ground anchor, leaning along one vector that morphs continuously from a fixed
"Niehues oblique" lean (tilt 0) to the true perspective projection of world-vertical (tilt 70). The fragment
finds the trees that can cover it by walking the few grid cells along the segment `fragment - s*lean` (mapped to world
via `dFdx/dFdy(v_world_position.xy)`), nearest-first, with 1 px analytic AA. Tree spacing is
`max(real spacing, ~7-9 logical px * metres-per-pixel)` snapped to an octave ladder (cell = 4000 m / 2^k, which divides the
100 km world-position wrap exactly), so the **tree count per screen area is bounded independent of zoom**; where a cell gets
smaller than ~2.5-5 px on its most compressed axis (far field at tilt) the per-tree detail is multiplied away analytically
and only the zero-mean painterly canopy (Berann) is left. No instancing, no extra geometry, no per-tree CPU data.

Why this and not instanced geometry: it inherits depth, bend, fog, the translucent hillshade veil, snow, contours and
land-cover masking for free (billboards/cones standing above the ground fail the veil's depth test, see 2.4), has no tile-LOD
popping, and its cost is a pure function of pixel count (predictable, tunable by one quality knob). Its weaknesses are real
and listed in 8 (no silhouettes above the ground mesh, forest-edge overhang, ~150-350 ALU/px estimate in the near field).

**Decisions needed from Sebastian are in 10.** Top risks in 8.

---

## 1. What the code base gives us (facts I relied on)

| Fact | Where |
|---|---|
| Forest = flat polygon, filter `landuse: forest` / `natural: wood`, colour `global.color.forest` (`hsl(112,16%,52%)`), style default `unlit-polygons` | `assets/scenes/stylus-osm.yaml:91, 1703-1705` |
| `unlit-polygons`: `terrain_grid: 128` tessellation, `color:` block with wall-rock, Snow Mode (`applySnow(col, 0.65)` for forest, identified by `distance(color.rgb,u_forest_fill)<0.02`), contours | `stylus-osm.yaml:895-980` |
| Piste areas are `lifted-polygons` drawn after forest (order 540 vs landuse 227), depth-lifted so they win against forest | `stylus-osm.yaml:1544-1556, 227, 235` |
| Fragment varyings: `v_world_position` = (position rel. view centre) + `u_map_position.xy` wrapped `mod 100000` (`TANGRAM_WORLD_POSITION_WRAP`), z = TRUE elevation (not bent); `v_position` camera space | `tangram-es/core/shaders/polygon.vs:149-154`, `core/src/gl/shaderSource.cpp:69`, `terrain-3d.yaml:107-113` |
| Terrain bend is `position.z -= terrainBendDrop(position.xy)` in the vertex stage only; world xy/z varyings unaffected | `terrain-3d.yaml:113`, `elevation.yaml:490-499` |
| Fog is a `filter:` block (runs after `color:`), so anything we put in `color:` is hazed automatically | `terrain-3d.yaml:30-55` |
| Hillshade veil: separate translucent pass (order 599) over all land cover, depth-tested against terrain; gives macro light incl. tilt-dependent warm/cool | `hillshade.yaml:379-440`, `stylus-osm.yaml:19` |
| Sun: `screenAnchoredSunDir()` / `tiltedSunDir(az2d,el2d,az3d,el3d,tiltMorph)`, `terrainTiltMorph()` (0 at zenith .. 1 at 45 deg pitch) | `elevation.yaml:464-487, 503-505` |
| Snow: `applySnow(col, cover)`; derivatives inside, so must be called in uniform control flow | `elevation.yaml:669-695` |
| Closest precedent: `rock-texture` fragment-only procedural material, analytic per-octave AA from `dFdx(v_world_position)`, baked tileable noise `assets/textures/rock_ridged_noise.png` (1024^2 RGBA8, sampled as `rockSampleAniso`) | `stylus-osm.yaml:1214-1350, 1591`, `docs/rock-texture-v2-plan.md` |
| Per-style GPU timers already exist: `PROFILE_GPU("style-gpu:<style>")` | `tangram-es/core/src/scene/scene.cpp:842`, `docs/profiling-plan.md` |
| Mesh draw path is `glDrawElements(GL_UNSIGNED_SHORT)`/`glDrawArrays` per tile mesh; **no instancing anywhere in core** (glad has the entry points, `mesh.cpp:185-215` never calls them); polygon vertex = `i16vec4 pos(+layer) + i8vec3 normal + abgr + selection` (+u16 uv) | `tangram-es/core/src/gl/mesh.cpp`, `style/polygonStyle.cpp:25-65` |
| Tile data has **no tree attributes**: landuse features carry only `landuse`/`natural`/`leisure`/... | `scripts/tilemaker/process.lua:655-668` (no `leaf_type`, `leaf_cycle`, `genus` anywhere in `scripts/tilemaker/*.lua`) |
| WorldCover is available as a raster source (ESA 2021, 10 m, max_zoom 14) used as a `translucent-polygons` earth style | `assets/mapsources.default.yaml:40-47, 278-289` |

Precision note: because of the 100 km wrap, `v_world_position.xy` has ~1 cm float resolution, enough to hash cells >= 1 m.
The ladder `cell = 4000/2^k` keeps `100000/cell = 25*2^k` an integer, so the pattern is seamless when the wrap shifts
(the view centre crossing a 100 km line changes `local_origin` by exactly 100 km = an integer number of cells, so
hash(cell mod N) must be used if cells are hashed by index; the prototype hashes the unwrapped index, which therefore
jumps once when the wrap flips - fix: take `cell mod (25<<k)` before hashing; one line, noted as TODO in step 2).

## 2. Candidate techniques

GLES 3.0 constraints (all apply to Android/iOS): no geometry shader, no compute, no SSBO/image load-store, no tessellation, no
`gl_PrimitiveID`/`gl_Layer`; **available**: instancing (`glDrawElementsInstanced`, `gl_VertexID`, `gl_InstanceID`),
integer ops and `uint` hashing in both stages, `dFdx/dFdy`, MRT, 3D/array textures, `textureLod`, transform feedback, vertex
texture fetch (>= 16 units typical), `discard`, alpha-to-coverage (only with MSAA). Rules out: GPU-side generation of
per-forest tree lists (needs compute/geometry), GPU-driven culling, indirect draws. Instancing is "available" but this engine's
mesh layer does not expose it - any instanced option costs a new C++ draw path (`MeshBase`, `Style`, uniform/raster binding per tile).

Worst case used for all numbers: **1920x1080 physical px (and 3840x2160 for HiDPI), 70 degree pitch, terrain bend on, forest
to the horizon, ~100-500 km2 of visible forest, 3 tile zoom levels of data in view.** In pixels: the lower ~45 % of the screen is
"near/mid" forest (trees resolvable), the upper ~35 % "far" (canopy only), ~20 % sky.

### (a) Fragment cell-trees in the forest polygon (RECOMMENDED core)
- Math: section 4/5, prototype `trees_frag.glsl` (`treeLevel`, `evalTree`, `sdTri`, `sdBlob`).
- Cost/pixel: candidates per pixel = cells visited by the lean segment (~ `aspect*cos(g)/...`, 3 at tilt 0, up to 8 capped) x up to 4 (cell + 3
  neighbours across the near edges) = 6-20 `evalTree` calls, each ~25-40 ALU (1 PCG hash x3 words, 2 mat2 mults, one sqrt/normalize, SDF,
  shading) -> **150-350 ALU/px**, early-out when coverage saturates (nearest-first), typically 40-60 % of worst case.
  Textures: 0 in the tree path, 1-2 in the canopy path. Far field: canopy path only, ~40 ALU + 2 fetches.
- Frame cost estimate (paper, 100 % of pixels in the near tree path): 2.07 Mpx x 250 ALU = 0.52 G ALU-ops; a UHD 630-class iGPU does
  ~200-250 G simple ops/s -> ~2.2-2.6 ms; a mid mobile GPU (Adreno 6xx / Mali-G7x) at 1080p with mediump sprite math ~1.5-4 ms.
  Realistic share of pixels in the tree path at 70 degrees ~ 35-45 % -> **~1 ms iGPU, 1-2 ms mobile at 1080p; x4 at 4K physical**.
  At tilt 0 (whole screen forest) it is the full 2.5 ms: this is the case to budget for.
- Memory: 0 extra (optional 1 MB noise texture shared). Vertex/instance counts: unchanged from today's forest mesh.
- Depth/draping/bend/veil/fog/snow: trees are *pixels of the ground polygon*: same depth, so the veil, fog, contours, snow, `lifted-polygons`
  pistes all work with no depth fight. Bend: invisible to it (world xy from varyings; screen Jacobian from `dFdx` already contains the bend's effect).
- AA / shimmer: sprite SDF is in screen px, so coverage `clamp(0.5 - sd, 0, 1)` is an exact 1 px box filter; tiers fade by pixel height; cells fade by `vis` (below);
  hash is world-anchored (no swimming on pan/rotate); only zoom crossfade changes pixels.
- Temporal: stable. Known artefact: `dFdx` of a 2x2 quad is a coarse, piecewise-constant Jacobian -> tiny pixel-pair-aligned
  jitter on sprite edges at grazing angles; mitigation = compute `J` analytically from varying planes at the vertex stage (pass `dWorld/dClip` as a varying) if seen.
- Cannot: draw outside the polygon (edge overhang, skyline silhouettes), correct depth for tree pixels standing in front of lower terrain behind them.

### (b) Instanced/vertex-generated billboards or cones, jittered world grid, no per-tree CPU data
- Needs: one new C++ draw path (instanced quad/cone, `gl_InstanceID` -> cell), a coverage mask to cull cells outside forest, and per-frame LOD choice.
  The mask is the hard part: a per-tile forest mask raster (8-bit, bake in tile worker, 64-128^2) or an offscreen screen-space mask pass; the vertex shader fetches it
  (vertex texture fetch) to collapse out-of-forest instances to a degenerate triangle.
- Counts at worst case: bounded-density law (spacing ~9 logical px) -> 1080p: ~25 K trees near field + horizon band after LOD (3 levels) ~ 40-60 K trees; x4 verts = 240 K verts,
  trivial (<0.2 ms). 4K: 4x.
- Fragment: sprite is a tall bbox (9 x 23 px for a 9 px cell) = 2.5x overdraw vs cell area, plus real overlap ~1.5x: ~4x fragments x ~40 ALU (SDF + shade, no search) = ~160 ALU/px-equivalent
  + depth writes. Similar ALU to (a), more bandwidth; alpha edges need MSAA/alpha-to-coverage or sorting (no sorting possible without compute).
- Interaction: trees stand above the ground so they **fail or ignore the hillshade veil** (the veil is drawn at terrain depth with `TERRAIN_DEPTH_SHIFT`; closer tree fragments
  are not shaded by it, producing the same "unshaded patches" class of bug documented in `docs/terrain-drape-*`), have to implement fog/snow/bend/lighting themselves, and
  sit in a different draw order from labels/trails. Tile-boundary LOD: trees would pop or double at tile zoom changes unless the world-anchored grid is global (then it is the same as (a)'s hash, plus the mask).
- Pros that (a) lacks: true skyline silhouettes, true depth, overhang over polygon edges, closer to "real 3D" at street level.
- Verdict: second place. Reserve for a possible *close-up (z >= 17 or pitch-with-low-altitude) upgrade*, not the main path.

### (c) Pre-baked atlas / octahedral impostors
- Octahedral impostors (Brucks 2018; Bruneton & Neyret 2011; and the recent open-world impostor write-up, see Sources) = N x N view-direction frames, blended by quantised view dir. For a map with a single
  camera-azimuth-independent tree look and 1-2 species, 8 views of a 64x128 cone is ~1 MB (RGBA8 with normal+depth ~2 MB) - cheap in memory. But they are a *geometry-side* (billboard) technique: all the integration costs of (b),
  plus view-dir quantisation popping, and the style requires hand-painted/rendered bake pipeline.
- A tileable baked "forest swatch" texture on the ground (no billboards) is the cheapest per pixel (1-2 fetches, free mip AA, ~10 ALU) and is an excellent **far-LOD / Berann** layer, but looks like paint on the floor
  when tilted (no occlusion, no standing-up, visible tiling at grazing angles), so it cannot carry the 2D->3D requirement alone. Used here only as the canopy source (section 5; `canopy_frag.glsl`).
- Verdict: use the *texture* idea only as far LOD; skip impostors.

### (d) Hybrid LOD ladder (what the recommendation actually is)
far = painterly canopy (noise, zero-mean per-octave fade), mid/near = (a) cell trees, optional near-geometry = (b) later. This is (a)+(c-texture), with (b) as an optional extra rung.

### (e) Others
- **Shell / fur texturing**: N offset copies of the polygon; each shell draws the tree cross-section at that height (disc radius `R(1-v)` in the own cell; only 1 cell lookup per shell fragment, ~25 ALU). N = 6-8 shells needed to hide
  stripes at grazing view -> 150-200 ALU/px *plus* Nx vertex load (grid 128 x N), Nx bandwidth (alpha test/blend), and depth/veil interaction again (shells are above ground). Same ALU as (a), much worse bandwidth and mesh cost. Rejected.
- **Relief / parallax-occlusion on a lifted canopy sheet**: lift the forest polygon by canopy height (vertex shader) and ray-march *down* into a height field. Gives true skyline silhouettes at ridges. Costs: a ray march with dependent reads (or the same cell hash), walls at polygon edges (could use tangram `extrude`, but that fights the terrain-draped vertex path),
  grazing-angle POM artefacts, and depth-bias conflicts with the veil. Interesting v3 research; not v1.
- **Screen-space tricks**: draw forest into an offscreen layer and blur/outline: kills crispness and doesn't solve placement. Rejected.
- **Impostor spheres** for deciduous (a ray-sphere test per candidate) are what `sdBlob`/sphere-normal in (a) already approximates analytically. Included in (a).
- **CPU-generated sprite meshes per tile** (tree list built in `tileBuilder` like `buildPolygonGrid`): easy with existing mesh path (no instancing needed, 4 verts/tree, uint16 indices -> 16 K trees per sub-mesh),
  but tree density is fixed at tile build time (zoom steps -> density pops), build cost scales with area (the tile pipeline is already the CPU bottleneck per `docs/tile-pipeline-perf-plan.md`), and it has all (b)'s depth/veil problems. Rejected.

### Ranked comparison

| # | Technique | ALU/px (near) | Extra mem | New C++ | Veil/fog/snow/depth fit | Shimmer risk | Horizon forest | 2D->3D continuity | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | (d) hybrid = (a) cell trees + canopy far LOD | 150-350 near, ~40 far | ~1 MB tex (optional) | none (steps 1-4) | excellent (same pixels as ground) | low (analytic) | canopy mass, no skyline trees | one lean vector, exact | **do** |
| 2 | (b) instanced cones/billboards + mask | ~160 equiv + overdraw | mask raster ~64 KB/tile | big (instancing, mask) | poor (veil depth, own fog/snow) | medium (alpha edges, LOD pop) | best (silhouettes) | OK | later, maybe close-up only |
| 3 | pure canopy texture only (c-texture) | 10-40 | 1 MB | none | excellent | very low | good (Berann) | no standing trees at all | as far LOD of #1 |
| 4 | per-tile CPU sprite meshes | ~100 | verts per tile | medium | poor | medium | OK | OK | no |
| 5 | octahedral impostors | ~80 + atlas fetches | 1-2 MB | big | poor | medium (view quantisation) | good | needs bake | no |
| 6 | shell texturing | 150-200 x overdraw | none | medium | poor | medium | OK | good | no |
| 7 | POM on lifted canopy | 200-400 (dependent reads) | none | medium | risky | medium | very good | good | research v3 |

## 3. 2D -> 3D as one parametrisation

Every tree is described only by its **ground anchor** (cell centre + jitter, world xy on the draped surface) and **three metre-lengths**
(height `h`, base radius `r`, spacing `s`). Screen shape is derived by two vectors:

```
lean_px  = h * leanPxPerM            // where the tip lands relative to the base, in screen px
R_px     = r / |J * e_across|        // base half-width px across the axis
leanPxPerM = mix( (0, cos(g_obl)/|Jx|),  projectedUpPxPerM(v_position),  m )
m        = smoothstep(pitch0 ~ 10 deg, pitch1 ~ 40 deg, camera pitch)
```
- `g_obl` (oblique "camera elevation" for Niehues, `cos g = 0.75` -> ~41 deg) is the fixed lean used when looking straight down; it is a *style constant*, not geometry. Direction on screen is exactly screen-up (trees stand upright on screen whatever the map bearing, as Niehues drew them).
- `projectedUpPxPerM` is the true perspective derivative of "world +z" at the fragment (prototype: from `u_view`, `u_proj`, `v_position`), which at 70 degrees pitch gives long vertical trees whose ground rows compress to `cos(pitch)` of their width: the forest becomes a wall of overlapping trees exactly as a real oblique view would - that is the Berann look *for free*, without a second algorithm.
- `m` is smooth, derivative-free of discrete modes; at m=0 the picture is a pure 2D sprite field (the cheapest case: 3 cells per walk), at m=1 it is a correct perspective 3D forest.
- Shading uses the **same** two angles: the sprite's camera elevation `g = mix(g_obl, 90deg - pitch, m)` is used to build sphere/cone normals in the (right, away, up) frame, and the sun comes from `tiltedSunDir(135,35.3, 200,38, terrainTiltMorph())` converted to that frame, so lit side swings from upper-left (2D) to front-left (3D) in lock-step with hillshade and rock-texture (they call the same function).
- Cast shadows: 2D/low tilt only (section 6) with direction `-sun.xy` on the ground.

### Size / spacing law (screen-bounded cost)

```
fpGeo  = sqrt(|det J|)                       // metres per px, isotropic
target = max( s_real/cos(lat), spacing_px * fpGeo )      // world metres between trees
L      = log2(4000 / target); k = floor(L); s_k = 4000/2^k   // s_k in [target, 2*target)
h      = aspect * s_k ; r = 0.34 * s_k      // aspect: conifer 2.4-3.0, broadleaf 1.2-1.5
```
- `s_real` = 6-7 m (managed stands; wild forest is sparser but the cell hash can thin it): below the zoom where `spacing_px*fpGeo` reaches 7 m (about z16 at 49 N with 8 px: 8 px * 2.4 m/px/... use the real number at tune time) trees are *real size and grow with zoom*; above it they are *stylised screen-sized* (8-16 logical px apart, 20-45 px tall conifers on a 1x display) - the "far larger than real" regime Niehues needs.
- Tree density in screen area is therefore always between `1/(2*spacing_px)^2` and `1/spacing_px^2`: with spacing_px = 8 logical px, 1080p/1x: 8 K - 32 K trees on screen; tree count stays bounded at every zoom. The cost per pixel is independent of zoom because the number of cells per pixel-walk depends only on `aspect`, not on `s_k`.
- Octave step (every x2 of zoom): the finer level (4x trees) is evaluated *together* with the coarse one only for `fract(L) > 0.7` (cross-fade `tf`); cost in that band is 2x (about 30 % of the zoom axis, only during zooming). Alternative: dither-dissolve by hash instead of linear mix to avoid grey blends; decide at look time.
- Between level switches trees grow with zoom physically (fixed world spacing); that is what a map reader expects, and the switch is a 1 -> 4 split of each tree that cross-fades.

### Per-tree detail LOD by footprint (no shimmer)
All detail multiplied by an analytic visibility computed from **px size**, never from time or distance guesses:
- cell visibility `vis = smoothstep(2.5, 5.0, s_k / fpMax)` (fpMax = world m/px on the most compressed axis; at tilt the row pitch in px is `s_k/fpMax`), `vis -> 0` selects the canopy path completely;
- tiers visible only if tree height >= ~14-24 px (`tiers = clamp((H-14)/10,0,1)`);
- light/dark halves visible if tree width >= ~5 px; below that the tree is a flat mid-tone dot fading to the canopy mean colour, which is chosen to equal the mean of the tree path (energy conservation, so the dissolve has no brightness step). **Calibrate this mean in step 2 by sampling both paths on a test card**; I did not compute it.
- hard-edged vs blended: ground colour between trees = `canopy * 0.72` (dark gaps like image 52) so the mean is preserved.

## 4. Species, shapes, shading, colour

**Conifer** (images 50, 53): isosceles spire, half width `R(1-v)` times a 5-tier sawtooth `(1 - 0.28*tiers*fract(5v))`, base radius
`0.34 s`, aspect 2.4-3.0 (narrow = Niehues, "tall narrow cones"). Cone normals `(x, -sqrt(1-x^2), 2.5*R/H)` with `x = across/halfwidth`; two-tone via
`smoothstep(-0.06, 0.06, N.L - 0.15)` (hard terminator ~1 px = Niehues) and a `bands` slider that widens the smoothstep for a softer Berann read. Snow cap: lerp the top 35 % of the lit side to snow white (below).
**Broadleaf** (images 54, 55): union of 3 discs (main + two hashed lobes: "cauliflower"), centre at 0.5 H, aspect 1.2-1.5, sphere normal `n = (a.x, -sinG*z + cosG*a.y, cosG*z + sinG*a.y)`, same lit/shade ramp, plus a leaf-clump highlight from the baked noise (1 fetch, only when the crown is > 10 px).
**Mixed**: per-tree Bernoulli on `conifer_fraction` (`u_tree.w`) from the hash, so a 0.3 value is 30 % spires between blobs; the fraction itself is low-frequency noise over the base fraction (patches, not salt-and-pepper).

**Colour**: base = `global.color.forest` (already dark/desaturated "conifer"). Per tree `tone = mix(0.62, 1.12, lit) + (r-0.5)*0.18` (value jitter +/-9 %), shadow side pushed cool `*(0.9,0.98,1.08)`, rare warm tuft `*(1.06,1,0.82)` (larch/birch, 22 % x lit side), global `contrast` slider (`u_tree.z`) mixes back toward the flat fill. The hillshade veil then adds the warm/cool lighting on top, as for all land cover. For broadleaf use a second palette entry `forest_broadleaf` (+yellow-green, +value) in `stylus-osm.yaml`.

**Snow Mode**: refactor `applySnow` into `snowAmount()` (returns the scalar `gate*stick*cover`) + the colour mix, so trees can use it: ground between trees `applySnow` as now (cover 0.65 -> raise to ~0.9 when trees are drawn, because the gaps are open snow); trees get `snowCap = snowAmount * smoothstep(0.55, 0.85, v) * lit` on conifers (white tips on the lit side; the shadow side stays green-blue), blobs get `snowAmount * smoothstep(0.1,0.6, a.y) * lit`. Derivative caution: call `snowAmount()` before any per-tree branching (it uses `dFdx(v_world_position)`).

**Forest edge**: v1 = clean clip at the polygon boundary (tree bodies get cut, a hard line like a hedge; Niehues also draws hard forest/clearing edges, so this is acceptable). Known
defect: trees whose base lies outside the polygon cannot overhang, and near-side trees at a south-facing edge look decapitated against the lifted piste polygon painted later. v2 = a **fringe pass**: draw the boundary as a *line* of width W with the same
shader; the line's lateral coordinate is a free signed-distance and the tree-base inside-test is a linearisation `lat(base) = lat(q) + grad(lat) . (base - q)` (gradient from `dFdx/dFdy`), so tree tips can cross the boundary by up to W. YAML-only if the line style exposes the lateral coordinate (needs a one-time check of `polyline.vs` varyings; not verified). v3 = a forest mask pass.

**Clearings, pistes, roads**: pistes are separate depth-lifted polygons (`lifted-polygons`) painted after the forest and win by depth shift, so the forest shader simply does not show under them - **no mask is needed** (cost: the forest fragments under a piste still run the tree shader first; overdraw ~1.1-1.3x, negligible). Forests with `natural=grassland` / `meadow` inner polygons overlap the same way (later order wins).

**Cast shadows** (2D / low tilt only, Niehues' long blue shadows): in the same `evalTree` loop, add a second cheap coverage `sdSegment(ground point, base, base + shadowDir*len, taper)` evaluated in world metres for the cells within `shadowLen/s_k` cells along `-sun.xy` (typical 2-3 cells x 4). Shadow colour = ground * (0.55, 0.62, 0.8). Cost +30-40 % at tilt 0; faded out with `m` (Berann panorama has no cast shadows, it uses canopy darkening in valleys), so it never meets the expensive grazing case. Not prototyped (v2 of the plan).

## 5. Far field: Berann painterly canopy (`canopy_frag.glsl`)
- 2 octaves of the existing tileable noise texture (pattern of `rock_ridged_noise.png`): `tuft*4` clump and `tuft*1.5 x 6` **stroke** stretched along the contour direction (perpendicular to `terrainGradient()`, so strokes follow the terrain), both
  **zero-mean and faded by `octaveFade(feature, footprint)`** so the mean colour never changes with distance (shimmer-free, same rule as the rock shader).
- Colour ramp: dark teal-green in shadow -> forest -> ochre/yellow at lit tufts (reference 51) using `sunLit` from the hillshade-compatible sun.
- This is also what the polygon shows with `forest_trees: off`: step 1 of the plan ships the canopy alone.
- Aerial perspective, contours and snow are applied after by the existing blocks (fog is in `filter:`).

## 6. Data: what can drive conifer/broadleaf/density/height

- **Currently nothing**: `process.lua` emits `landuse`/`natural` only (lines 655-668). OSM carries `leaf_type` (`needleleaved|broadleaved|mixed`), `leaf_cycle` (`evergreen|deciduous|mixed`), `genus`, `species`, `wood` (old), `height`. **Coverage I cannot verify here**: in my knowledge it is present on a minority of wood polygons (high in some European countries, tiny elsewhere) **[A]**.
- Server-side change (tilemaker, YAML-free for the client): `if landuse=="forest" or natural=="wood" then Attribute("leaf_type", Find("leaf_type")); Attribute("leaf_cycle", Find("leaf_cycle")) end` (+ optional `genus`). Cheap; adds bytes only on tagged polygons. The client then picks a fill colour per type (`forest`, `forest_conifer`, `forest_broadleaf`) and the shader recognises the species by the same `distance(color.rgb, fill) < 0.02` trick the contour/snow code already uses - **no C++ and no new vertex attribute**. (Existing checks for `u_forest_fill` in `unlit-polygons` must also accept the two new fills.)
- Raster: WorldCover class 10 (tree cover) tells *where* trees are (OSM forest polygons miss many); no species. Copernicus HRL Dominant Leaf Type (EU, 10-20 m, broadleaved/coniferous) would give species where OSM is silent; canopy-height rasters (global ~10 m products, ETH/Meta-WRI) would give `h` and a density (cover). All need server-side ingestion into tiles (vectorise or serve as a raster layer like elevation). Out of scope for v1; flagged as the way to get *real* forests at z15+.
- **Free prior from the DEM + latitude (YAML-only fallback)**: treeline `T(lat) = 4000*(1-(|lat|/70)^1.8)` m (about 2100 m at 46 N, 1860 at 49.4 N, 970 at 60 N: rough, **[A]**, expose `treeline_offset` like `snow_line_offset`); conifer fraction rises with elevation, `p_c = mix(0.25, 0.95, smoothstep(0.35 T, 0.85 T, elev)) + boreal bonus for |lat| > 55`; tree height/density shrink toward the treeline (`h *= 1 - 0.6*smoothstep(0.8T, T, elev)`, spacing thinning by hash so the edge goes krummholz-sparse); beyond `T` the forest fill fades to alpine meadow *only if* the polygon asserts forest (we do not delete OSM forest, we thin it); aspect: warm (equatorward) slopes keep broadleaf to higher elevation (+150 m). All use `v_world_position.z` and `terrainGradient()` that `applySnow` already uses.
- Trail/piste "readability": none of this touches data; see section 7 guardrails.

## 7. Guardrails (map, not video game)
- Palette is the existing `global.color.forest` +/- 9 % value, hue shift <= 8 deg cool/warm, saturation never above the base (cartographic: Imhof's forest tints are always subordinate to relief, labels and lines). `contrast` slider default 0.55.
- Detail energy fades with distance: `vis` (cells), `tiers`, canopy octave fades; the far field is the plain forest tone + painterly strokes. No outlines on trees (silhouettes by value only).
- Lines and labels: trails/roads are separate line styles drawn after polygons (opaque with casing); labels have halos. If a trail is still hard to read over spires, the contingency is a screen-space "calm" factor: lower `contrast` where `v_color` alpha / a **line-proximity mask** says so - a mask requires a mask pass (not planned); the cheap fallback is globally reducing tree contrast whenever `show_trails` (or `show_ski_runs`) is on, like `stylus-bike-hike` already toggles other things.
- Height cap: `aspect` clamps to <= 3.0 and tree height in px is capped at ~50 px logical so a spire never dwarfs a label.
- Voxel/Pokemon cuteness is avoided by *no outline, no saturated greens, no random scale outliers beyond +/-15 %*; it can be dialled with `jitter`, `bands`, `contrast`.

## 8. Performance budget, measurement, risks

**Budget (proposed, Sebastian to confirm):** at 1920x1080 physical, tilt 0, screen full of forest (worst for the tree path): **<= 2.0 ms added GPU on a UHD 630 / Xe-LP class iGPU, <= 4.0 ms on a mid mobile GPU (Adreno 618 / Mali-G76)**; at 70 degrees with forest to the horizon: **<= 1.5 ms / <= 3 ms**. Beyond that the auto-quality ladder (below) must kick in. My estimate: ~2.2-2.6 ms (tilt 0, iGPU), ~1 ms (70 deg) - **at the budget edge for mobile; unmeasured.**

Quality ladder (each a uniform, no recompile): (1) `MAXC` rows 8 -> 4 and neighbour checks 4 -> 2 (-45 % ALU, a few visible dropouts), (2) disable cast shadows, (3) tiers/clumps off, (4) `spacing_px` 9 -> 12 (fewer, bigger trees; same per-pixel cost but less aliasing risk), (5) trees off, canopy only (~40 ALU). Mobile builds should default to rung 1-2 and `mediump` for the sprite math (hash stays `highp uint`).

**Measure with the repo's profiler** (`docs/profiling-plan.md`): `style-gpu:forest-trees` appears automatically (scene.cpp:842) as a separate style, so A/B is "same camera, style on/off". Needed new counters (`Profiler::counter`): `forestPxEstimate` (sum of tile forest polygon area in px^2 at the current zoom: cheap on CPU from tile polygon bounds; gives ms/Mpx), `forestLevel` (current `k` and `fract(L)`, to correlate spikes with the cross-fade band), and a one-shot debug define `TREES_HEATMAP` that outputs `evalTree` call count as colour (for **Sebastian's eyes** / not for me). Method: Release build, fixed views (Alamo-like 2D forest, Lions/Cypress BC `49.38 -123.20 z13`, Zermatt, plus a 70 degree view toward Whistler-like horizon), 300 frames each, trace summary median GPU ms. **Do not trust numbers from the contaminated 70 ms baseline** - redo the baseline on a clean machine first.

Risks, most to least likely to bite:
1. **ALU count is a guess.** 150-350 ALU/px may be 2x off; `dFdx` derivatives, loops with data-dependent break (divergence at tree edges, cheap on modern GPUs but real on older mobile), array indexing `cs[k]` in the prototype (use unrolled code on mobile compilers).
2. **Overdraw of the forest polygon itself** (forests under later polygons, `earth` at 599, lifted pistes) multiplies the tree cost; the `unlit-polygons` baseline is already the top GPU zone. If it matters: draw forests *last among opaque covers* with depth test on so early-Z kills hidden fragments (needs an order change - check tangram's sort/depth mode first).
3. **Hillshade veil is a full-screen translucent pass over everything** (already ~50 ms in the trace). Trees do not add to it, but if the veil cost is real, tree "gain" disappears into it; look at it first.
4. **Derivative noise** at grazing angles (coarse 2x2 quad `dFdx`) -> flicker at the sprite edge; fallback is a vertex-computed Jacobian varying.
5. **Zoom cross-fade band doubles cost** (30 % of the zoom axis); mitigate by dither-dissolve with early-out per hash.
6. **No silhouettes above terrain, no overhang at edges**: the image can look "painted on", especially on ridgelines at 70 degrees; mitigations v2/v3 (fringe line pass, lifted canopy).
7. **Float range**: 100 km wrap seam (section 1) and `ivec2(floor(P/sk))` for the smallest cells (3.9 m x 25*2^11 cells... fine in int32).
8. **Mobile ES 3.0 uint ops/pcg** performance is fine on modern parts but ancient Mali-400 class is out of scope anyway (GLES 3.0 target).
9. **Tile LOD seams**: terrain tiles of different zoom share the same world-anchored hash, so no seam *unless* a tile's polygon grid is coarser and its `dFdx` Jacobian differs; cell boundary at tile boundaries should be invisible by construction. Look at tile seams in step 2.

## 9. Staged implementation plan
All steps default OFF (`global.forest_style: flat` or a gui variable), one commit each on master (no worktrees, per the memory note), reviewed by Sebastian's eyes.
GUI plumbing: `global.gui_variables` exactly like the other sliders (rock/hillshade use this; pattern in `hillshade.yaml` gui blocks).

| Step | What | Files | C++? | Flag / sliders | Look-at |
|---|---|---|---|---|---|
| 0 | Clean-machine baseline GPU zones (unlit-polygons, hillshade) + `forestPxEstimate`/`forestLevel` counters | scenes (none), `tangram-es/core/src/scene` for counters | tiny C++ | - | profile traces |
| 1 | `forest-canopy` style: Berann canopy only (zero-mean, octave-faded, contour-aligned strokes), still the old polygon | `stylus-osm.yaml` (new style mixing `unlit-polygons`, draw rule on `layers.landuse.forest`), shader from `canopy_frag.glsl` | no (YAML+shader, reuses `rock_ridged_noise.png` sampler setup) | `forest_style: flat\|canopy\|trees`, `canopy_contrast`, `canopy_stroke` | tilt 0 and 70 over Lions/Cypress |
| 2 | Cell trees, conifer only, tilt 0 (oblique lean m=0), no shadows, clean clip, `vis`/octave ladder, seam-safe hash (`mod 25<<k`) | new style `forest-trees`, shader from `trees_frag.glsl` | no | `tree_spacing_px`, `tree_aspect`, `tree_contrast`, `tree_bands` | 2D: `z13..z16` sweep, check popping on zoom |
| 3 | True-perspective lean (`m` morph) + sun swing + quality ladder uniform + heat-map define | same file | no | `tree_lean_morph_range`, `tree_quality` | tilt 0 -> 45 -> 70 ramp, horizon view |
| 4 | Broadleaf blobs + mixed fraction + DEM prior (treeline, elevation conifer fraction) | same + `elevation.yaml` helper `treelineAt()` | no | `tree_conifer_fraction`, `treeline_offset` | Zermatt, BC coast, Black Forest |
| 5 | Snow Mode caps (`snowAmount()` refactor), snow in gaps | `elevation.yaml` (refactor), `stylus-osm.yaml` | no | reuses Snow source | stylus-snow at 46 N |
| 6 | Cast shadows (2D/low tilt) | shader | no | `tree_shadow` | tilt 0 snow/ski area |
| 7 | Server data: `leaf_type`/`leaf_cycle` attributes -> `forest_conifer` / `forest_broadleaf` fills | `scripts/tilemaker/process.lua` (tile regen needed), `stylus-osm.yaml` filters + fills, update the `u_forest_fill` checks | server only | - | regions with tags |
| 8 | Fringe pass (tips over the boundary) | `stylus-osm.yaml` line style | maybe (needs lateral varying) | `tree_fringe_px` | piste/forest boundaries |
| 9 | Optional: instanced near-field (b) | C++ mesh/instancing | big | - | only if 2D->3D at z17+ looks flat |

Each step: verify off-switch = byte-identical rendering of today (the draw rule uses the old style when `forest_style: flat`).

Build and look commands (CLAUDE.md; this repo has no extra worktrees, run from `/home/sebastian/projects/maps`). After any change: `make` and `make DEBUG=1` must both compile clean, then hand over:
```
cd /home/sebastian/projects/maps
# Lions/Cypress, BC - forest, top-down:
./build/Release/ascend --view.lat 49.38 --view.lng -123.20 --view.zoom 14 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat 49.38 --view.lng -123.20 --view.zoom 14 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
# same place, tilted horizon forest (tilt is the point of the test here, so non-zero tilt is deliberate):
./build/Release/ascend --view.lat 49.38 --view.lng -123.20 --view.zoom 13 --view.rotation 0 --view.tilt 70 --sources.last_source stylus-osm-terrain
# alpine cluttered terrain / treeline (Zermatt):
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
# Snow Mode: replace the last argument with: --sources.last_source stylus-snow
```
(Step 1 and later need the `forest_style` GUI variable switched from the in-app panel; the persistent config makes it sticky, so note its value when comparing.)

## 10. Decisions for Sebastian
1. Accept approach (a)+(d) (fragment cell trees + painterly far LOD), with the known limitation "no tree silhouettes above the ground mesh / no overhang" until steps 8-9?
2. Perf budget in section 8 (2 ms iGPU / 4 ms mobile at 1080p, tilt 0 full-forest) - or tighter ("absurdly fast")? A tighter budget means canopy-only (step 1) at tilt 0 on mobile.
3. Style call: tree lean in 2D (`cos g_obl = 0.75`, trees upright on screen regardless of bearing) vs rotating with the map; and conifer aspect (narrow Niehues spires 3.0 vs 2.4).
4. Should I request the server-side tilemaker attribute change (`leaf_type`, `leaf_cycle`) now, and is WorldCover/HRL ingestion worth a later phase?
5. Snowcaps: lit-side tips only (my default) or whole canopy lightly dusted?
6. Cast shadows in 2D: yes/no (adds ~35 % at tilt 0).

## Sources
- Prototype math: `trees_frag.glsl`, `canopy_frag.glsl` (scratchpad `trees/`); `glslangValidator -S frag` passes (syntax/type only).
- Repo references as cited in section 1; precedent `docs/rock-texture-v2-plan.md`, `docs/terrain-lighting-v2-plan.md`, `docs/profiling-plan.md`, `docs/tile-pipeline-perf-plan.md`.
- Web (one search only, snippets not deeply read): octahedral impostor atlas write-up [cinevva "open world browser part 19 imposters"](https://app.cinevva.com/blog/2026-05-04-open-world-browser-part-19-imposters); [Journal of WSCG 2.5D impostors for forests](https://dspace.zcu.cz/items/9b6c354a-401c-4436-9ef9-fa6310baef50); [AMD "Real-Time GPU Tree Generation"](https://gpuopen.com/download/Real-Time_GPU_Tree_Generation.pdf); [Three.js octahedral impostor forum thread (lists Brucks 2018, Bruneton & Neyret 2011)](https://discourse.threejs.org/t/a-forest-of-octahedral-impostors/85735.md).
- From memory (not re-verified): Niehues/Berann panorama conventions, shell texturing (Lengyel 2001 "fins and shells"), POM (Tatarchuk 2006), Imhof forest symbolisation; OSM tag names `leaf_type`/`leaf_cycle`; treeline latitude figures.
