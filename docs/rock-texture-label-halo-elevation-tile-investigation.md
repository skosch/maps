# Peak-label halo flicker: elevation/tile-attachment instability investigation

**Status (2026-07-20): research only, no code changes made, no fix implemented**, per this
project's established "research first, present plan, implement only after sign-off" workflow.
Nothing was built or run. This document is a static code trace only.

**Please read this doc's "What's already refuted" framing below before acting on it -- the
headline finding here is not a confirmation of the assigned theory, it's a redirection to a
different, better-supported one that surfaced while investigating it.**

---

## 0. What this document was asked to check, and the short answer

Sebastian asked (after exhaustively ruling out the label/halo fragment shader itself,
`docs/label-halo-stroke-alignment-investigation.md` and
`docs/rock-texture-label-halo-flicker-investigation.md`) whether ELEVATION/TILE-ATTACHMENT
INSTABILITY -- specifically, a raster attachment flipping between different ancestor/LOD sources
frame-to-frame -- could be feeding the confirmed-correct background-adaptive halo mechanism
unstable input, and whether this could be the same mechanism as the already-parked "stuck
elevation shelf" bug.

**Short answer: the specific mechanism asked about (tile/raster LOD flips caused by camera
panning) is real and confirmed in code, but is provably NOT triggered by pure lateral panning at
`--view.tilt 0` -- the exact condition this project's test protocol always uses.** Chasing that
thread down turned up something else instead, sitting immediately adjacent to the very
`rock-texture` style whose `terrain_grid` fix opened this whole investigation: an **already
identified, already-partially-mitigated, but not confirmed-fixed floating-point hash-precision
bug in `rock-texture`'s own procedural noise shader** (`assets/scenes/stylus-osm.yaml`, currently
uncommitted working-tree content -- see Section 4). Its own in-code comment explicitly names
"corrupting the background-adaptive text halo's luminance read" as a suspected consequence. This
is a far more precise match for "even a single screen pixel of pan" than anything in the
elevation-attachment code, and it directly reconciles with the live 4-mode rock-texture recolor
test Sebastian already ran and that the sibling doc describes as "conclusively refuting" the
background theory (Section 5 explains how).

---

## 1. Confirmed: raster attachments CAN flip between ancestor/LOD sources with no data change

This part of the assigned question is real and code-confirmed, independent of the tilt-0 finding
in Section 2 -- worth stating plainly before qualifying it.

### 1.1 The per-frame healing pass can swap a tile's raster texture on any frame, for reasons unrelated to camera motion

`TileManager::updateTileSets()`'s per-visible-tile loop (`tangram-es/core/src/tile/tileManager.cpp:605-661`,
already instrumented by the earlier "elevation shelf" investigation's diagnostic, lines 663-709)
runs **every single call to `updateTileSets()`**, which itself runs **unconditionally on every
`Scene::update()`** (`tangram-es/core/src/scene/scene.cpp:676`, called before `viewChanged` is
even computed as a separate flag at line 672/685 -- i.e. it is not gated on the camera having
moved at all). Concretely:

- Line 639: `if (auto exact = srcs[ii]->getTexture(TileID(intended.x, intended.y, intended.z)))`
  -- a **cache lookup repeated every frame**. If the exact-zoom texture is not yet cached, the
  raster stays pinned to whatever ancestor (or empty) it currently has (line 649-660's ancestor
  walk). The moment that exact texture becomes available in the shared cache -- because
  *something else* (the elevation tileset's own display traversal, a neighbor prefetch, anything)
  populated it -- this loop swaps `raster.tileID`/`raster.texture` to the new value **on
  whatever frame that happens to be**, with no interpolation or fade.
- This is a genuine "attachment flip with no underlying data change" in the sense the question
  asked: the *real* DEM data at that coordinate never changed, only *which* cached texture object
  is bound this frame. The event's timing is governed by network/cache/task-completion order,
  **not by camera position** -- it can happen while the camera is perfectly stationary.
- `Style::setupTileShaderUniforms()` (`tangram-es/core/src/style/style.cpp:240-276`) recomputes
  the `u_raster_offsets`/`u_rasters`/`u_raster_sizes` uniforms **fresh on every draw call** from
  whatever `raster.texture`/`raster.tileID` happens to be attached at that instant
  (`style.cpp:262-270`: `x,y,z` derived from `tileID.z - raster.tileID.z`). This recomputation is
  internally self-consistent (no offset/texture mismatch *within* one frame), but confirms that
  whenever the attachment swaps between frame N and N+1, the shader samples a **different UV
  sub-region of a different texture** on N+1 than on N -- a real, instantaneous, code-confirmed
  discontinuity in whatever `getElevationAt()` (used by `rock-texture`'s own slope/curvature
  stencil, `stylus-osm.yaml:767-786`, and by `hillshade.yaml`'s `textureShading()`) reads for that
  tile.

### 1.2 `upgradeAttachedRasters`'s neighbor-aware `effectiveS` and the overzoom-composite rebuild are explicitly designed to re-trigger on a "displayed-zoom flip"

`TileManager::upgradeAttachedRasters()` (`tangram-es/core/src/tile/tileManager.cpp:936-1219`)
computes, for every vector tile with raster attachments, an `effectiveS` = the **max** of the
tile's own styling zoom `s` and every visible same-zoom neighbor's `s`
(`tileManager.cpp:997-1023`, added to fix a T-junction crack between neighbors landing on
different `s`). This feeds `livePrimary` into `RasterSource::overzoomTargetZoom()`
(`rasterSource.cpp:1000-1009`, capped at `primary.z + kOverzoomCompositeMaxLevels` (== 2,
`rasterSource.cpp:286`)) and into a per-cell zoom-cap grid (`tileManager.cpp:1065-1194`) that is
recomputed **every single call**.

`RasterSource::buildOverzoomElevationMosaic()`'s own comment is explicit about this being a
designed, handled case, not an oversight (`rasterSource.cpp:1056-1062`): *"Folding the target
zoom in means a displayed-zoom flip (either direction) changes the signature and triggers exactly
one re-stitch."* -- i.e. the code **already anticipates and is built to tolerate** `s`
(styling/overzoom zoom) flipping between frames, by cheaply detecting it via an FNV-1a signature
over each cell's `(target zoom, cached-or-not)` pair (`rasterSource.cpp:1063-1078`) and rebuilding
the composite exactly once per flip, not continuously. This is real and directly answers the
question's mechanism ("could a raster attachment flip between ancestor/LOD sources... without the
underlying real data actually changing") -- **yes, structurally, this is exactly what the code is
built to do when `s` changes** -- but see Section 2 for why `s` provably does not change from
pure lateral panning under this project's actual test condition.

---

## 2. Decisive finding: at `--view.tilt 0`, per-tile screen area -- and therefore `s` -- is provably invariant to panning

This is the load-bearing fact of this document, and it directly qualifies (and mostly rules out)
Section 1.2's mechanism as the trigger for "flickers as I pan by even one pixel," **specifically
under this project's own mandated test condition.**

`View::getTileScreenArea()` (`tangram-es/core/src/view/view.cpp:614-674`):

```cpp
if (m_pitch == 0 || !allGreater(a[3], glm::vec4(0))) return FLT_MAX;
```

(`view.cpp:665`). `Map::setTilt(_radians)` calls `impl->view.setPitch(_radians)` directly
(`tangram-es/core/src/map.cpp:663-667`) -- `--view.tilt 0` (this project's own CLAUDE.md-mandated
flag for every test in this thread) sets `m_pitch = 0` exactly. **At `m_pitch == 0`, this
function returns the literal constant `FLT_MAX` for every on-screen tile, completely bypassing
the actual screen-space quad-area computation** (the `ndcToScreenSpace`/`signedArea` code at
`view.cpp:667-673` is unreachable when pitch is 0).

Tracing what this does to `TileManager::updateTileSets()`'s LOD/overzoom decision
(`tileManager.cpp:276-338`):

- `float area = _view.getTileScreenArea(tileId);` (`tileManager.cpp:278`) is therefore a
  **position-invariant constant** (`FLT_MAX`) for every visible tile whenever tilt is 0.
- `area < effMaxArea*std::exp2(2*float(zoomBias))` (`tileManager.cpp:302`) is essentially never
  true (`FLT_MAX` dwarfs any real threshold), so subdivision is gated **purely by
  `stoppedByMaxZoom`** (`tileId.z >= maxZoom`, `tileManager.cpp:300-301`), not by any
  continuously-varying screen geometry.
- Once a tile does hit its source's max zoom and takes the overzoom branch
  (`tileManager.cpp:325-334`): `int s = tileId.z + max(0, ceil(log2(area/effMaxArea)/stepExponent))`
  is computed from the same constant `area`, so **`s` itself comes out to a fixed value for any
  given `tileId.z`/`effMaxArea`/`stepExponent`, independent of camera position** -- it is then
  clamped by `_view.getIntegerZoom()` (`tileManager.cpp:333`), which is also unaffected by lateral
  panning at constant zoom (only a real zoom-level change moves it).

**Conclusion: under `--view.tilt 0` and at a fixed zoom level, no tile's `s` can change from
panning alone, by even one pixel or by a full screen-width.** This means:

- Section 1.2's `effectiveS`-driven composite-cap recompute cannot be triggered by panning under
  this condition -- there is no "some neighbor's `s` crossed a rounding boundary because the
  camera moved a pixel" scenario at tilt 0, because `s` has no positional input to begin with.
  The mechanism the task asked about (an area/LOD-based flip caused by "panning by even a single
  screen pixel") is real *code*, but not reachable by that *trigger*, specifically at tilt 0.
- Section 1.1's cache-driven healing-pass flips remain real and possible at any time, including
  while stationary -- but their timing is tied to network/cache/task completion, not to the pan
  gesture. A correlation with "I was panning when I saw it" is therefore more plausibly
  **coincidental** (panning is when Sebastian is actively watching the screen and also the most
  common reason new tiles enter view and trigger fresh loads/heals nearby) than **causal** in the
  sense of panning itself flipping an already-resident tile's attachment.
- This is a testable, falsifiable prediction worth flagging to Sebastian directly (see Section 6):
  if the `s`/area mechanism were the true driver, the flicker's character should visibly change
  between `--view.tilt 0` and a small nonzero tilt (e.g. `--view.tilt 0.05`), since only the
  latter re-enables real position-sensitive area computation. If the flicker looks identical at
  both, that's further evidence against this mechanism and for Section 4 below.

**Not fully closed off**: this only shows `s` can't flip from panning *at a fixed zoom*. It does
not address whether entering/leaving the visible-tile set at the viewport's edge, or ordinary
tile-boundary crossing for a screen pixel very close to a real tile edge, has any residual
effect -- but a peak label sitting well inside frame (as in every test coordinate this project
uses) is not near a viewport edge, and ordinary tile-boundary crossing for a *fixed*, cached tile
is a deterministic, continuous coordinate change, not the kind of race this document was asked to
look for.

---

## 3. Hillshade auto-contrast: real cross-frame variation exists, but is deliberately damped -- not a match

`MapsApp::mapUpdate()` (`app/src/mapsapp.cpp:891-924`) drives `u_texture_shading_auto` from
`RasterSource::aggregateRugosity()` (`rasterSource.cpp:1141-1152`, averaging a `rugosity` field
across all currently live/cached elevation mosaics) and explicitly **slew-limits** the result with
a ~0.7s time constant (`mapsapp.cpp:894,908`: `texShadingAutoContrast += (target -
texShadingAutoContrast) * min(1, dt/0.7)`), only pushing the uniform (and requesting a render) when
the change exceeds `0.002` (`mapsapp.cpp:913`). The comment at `mapsapp.cpp:891-897` confirms this
is deliberate: *"panning shifts the target only gradually as tiles enter/leave the window... so
any remaining steps ease in instead of popping."*

This **is** a real mechanism by which the global hillshade appearance changes over time
independent of any single tile's own data changing (the aggregate statistic shifts as tiles
enter/leave the cache window) -- but it's damped on purpose specifically to prevent the kind of
per-frame pop this investigation is looking for, and it's scoped to the `hillshade` style (used
for terrain-ground shading), not wired into `rock-texture`'s own separate slope/curvature stencil
at all (`rock-texture`'s color block reads `u_rock_*` uniforms and a raw 5-tap
`getElevationAt()`/`u_raster_offsets` sample, never `u_texture_shading_auto`). **Ruled out as the
proximate cause of frame-to-frame flicker** -- real, but the wrong time scale and the wrong style.

---

## 4. What's actually sitting right next to `rock-texture`'s `terrain_grid` fix: an already-flagged, not-confirmed-fixed hash-precision bug

This is the headline finding, found while reading the exact style block this whole investigation
thread centers on. **`assets/scenes/stylus-osm.yaml`'s current working-tree diff (uncommitted,
`git status` shows it modified relative to HEAD) already contains its own analysis of a
mechanism that matches the reported symptom far more precisely than anything above:**

`rockNoise()`'s comment (`stylus-osm.yaml`, in the `rock-texture` style's `shaders.blocks.global`
block, present in the current uncommitted diff):

> "p is derived from v_world_position.xy (real Mercator-projected meters, hundreds of thousands
> to low millions in magnitude...) so i can be huge. rockHash's sin()-based hash loses precision
> catastrophically at large arguments (GPU sin() only reliably range-reduces over a bounded
> domain), making the hash hypersensitive to tiny input changes at this scale -- **a sub-pixel
> pan can shift the true (unwrapped) coordinate by a part-per-million yet flip the hash's output
> entirely, producing visible flicker/corruption exactly where this noise renders (the
> rock-texture polygon), and, downstream, corrupting the background-adaptive text halo's
> luminance read of whatever's under a label near a rock-textured area** (its color block
> re-samples the actual composited framebuffer, so an unstable rock-texture color feeds it
> unstable input)."

This is not something I'm inferring -- it's already written down, in the exact file this whole
thread has been circling, as a **named, understood risk**, with a partial mitigation already
applied: `vec2 iw = mod(i, 1000.0)` wraps the noise cell's integer coordinate into `[0, 1000)`
before hashing, specifically to keep the hash's input magnitude bounded.

### 4.1 Why the existing mitigation is plausibly incomplete

`rockHash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123); }` is called on
`iw` (post-`mod`), so its argument to `sin()` is `dot(iw, vec2(127.1, 311.7))` with
`iw.x, iw.y ∈ [0, 1000)` -- bounding the dot product to roughly **`[0, ~439000)`**
(`1000*127.1 + 1000*311.7 ≈ 438800`). This is dramatically better than the unwrapped case (which
could reach the millions), but it is **not** small: `sin()`'s hardware range-reduction precision
degrades progressively as its argument grows, and arguments in the hundreds of thousands are
still in a range where several real-world GLSL implementations (particularly older/mobile
drivers, but not exclusively) show visibly poor precision. The comment's own reasoning
(`mod(i,1000)` chosen specifically "to keep the hash's own input magnitude small and stable
everywhere") reads as a considered, good-faith mitigation -- but nothing in the surrounding code
or comments indicates this was **verified live** to actually eliminate the instability, only that
it was expected to help. The number `1000` was chosen for a *different* constraint (keeping a
single contiguous bare-rock feature within one hash period, to avoid a visible seam), not derived
from any GPU's actual `sin()` precision envelope.

### 4.2 This directly reconciles the sibling doc's "conclusively refuted" 4-mode test

`docs/label-halo-stroke-alignment-investigation.md` Section 0 describes a live test (since
reverted, not present in the current tree) where `rock-texture`'s fragment shader was forced
through four different output modes -- "raw noise / raw elevation / slope-only / pure procedural
noise with zero elevation dependency" -- and the halo flickered "identically regardless of which
mode was active," taken as conclusive evidence against *any* background-content-driven theory.

**That conclusion is airtight against elevation-DATA-driven instability (Section 1/2 above
already independently support this: `s` can't even flip from panning at tilt 0, so there wasn't
much elevation-driven variation for that test to suppress in the first place) but it does not, by
itself, rule out the noise hash.** If those four debug modes changed *what elevation-derived
signal* fed into the final color (raw elevation vs. slope vs. off), but did not specifically
disable the `rockNoise()`/`rockFbm()` call itself (there is no evidence either way in the sibling
doc -- the reverted diagnostic's exact code isn't preserved anywhere in the tree, and the "pure
procedural noise with zero elevation dependency" mode's own name suggests the *noise* was
deliberately kept running, with only its *elevation coupling* removed), then **all four modes
would still inherit rockHash's own instability unchanged** -- fully explaining "flickered
identically regardless of mode." This is a clean, coherent reconciliation of every observation in
this thread so far:

- It explains why forcing background content through four unrelated elevation-relationships made
  zero difference (the actual instability's source, the noise hash, was never touched by any of
  those four modes).
- It explains the granular, "even a single screen pixel" sensitivity precisely (a large-argument
  `sin()` hash is, by the nature of hardware trig range-reduction, exactly this pixel/subpixel
  sensitive -- a part-per-million change in a ~400,000-magnitude float can legitimately flip which
  side of a range-reduction wraparound the hardware lands on).
- It explains "gets stuck mid-flicker in a visibly wrong/corrupted state" -- a chaotic hash can
  render a stable-looking but *wrong* value for many consecutive frames whenever world position
  happens to sit still (even sub-pixel motion can be quantized away by the 1/4px position-scale
  truncation the sibling stroke-alignment doc already found, `textLabel.cpp:229-232` -- though
  that's for label position, not fragment `v_world_position`, the mechanism-shape is the same
  kind of "quantized then jumps" behavior), then jump again the instant a fresh pan nudges it past
  a precision boundary.
- It is **not** contradicted by anything already ruled out in this thread: it needs no elevation
  attachment flip, no fragment SDF issue, no background-halo luminance bug -- it is a
  self-contained instability in `rock-texture`'s own procedural color, upstream of everything the
  fragment-shader investigations already checked.

**This is inference, not a confirmed replacement finding** -- I did not see the reverted 4-mode
debug diagnostic's actual code, so I cannot confirm it left `rockNoise()` live in all four modes.
This reconciliation is the most internally-consistent account available from the evidence in the
tree, not a proven one.

---

## 5. Is this the same bug as the parked "elevation shelf" issue?

**Plausibly related, but not the same mechanism as the Section 4 finding, and not clearly the
same as each other either.**

- The "shelf" bug (`docs/rock-texture-elevation-shelf-investigation.md`) is about a raster
  attachment **permanently** stuck at the wrong (coarser) zoom for an entire tile's visible
  lifetime -- a static, load-once problem, confirmed structurally in Section 1.1/1.2's exact code
  paths.
- A shelf's edge sitting under a peak label absolutely would produce a real, sharp
  hillshade/`rock-texture` luminance discontinuity there -- and if that boundary happens to sit
  exactly under a label, the correctly-working background-adaptive halo would legitimately react
  differently on either side of it. But a *static* shelf, once settled, would not by itself
  produce *continuous* per-pixel-of-pan flicker -- it would produce a single, stable step in
  luminance that the halo crosses once as the label (or its sample footprint) moves past the
  shelf's edge, not a chaotic back-and-forth.
- Where the two *could* compound: if a tile is currently in the "ancestor-pinned, still being
  re-checked every frame" state (Section 1.1's healing loop, `numMissingRasters != 0`) rather than
  fully settled, its attachment is a live candidate to flip (heal) on some future frame per
  Section 1.1 -- at that moment, a shelf-adjacent label's background would take a real, discrete
  jump. This is consistent with, but not proof of, "the shelf bug manifesting as halo flicker
  instead of/in addition to floating landcover."
- This does **not** need Section 4's noise-hash theory to be wrong -- both can be true
  simultaneously (a rock-texture polygon can have both an unstable procedural color *and* sit
  near a genuinely-pinned elevation shelf); they are independent contributors to the same visible
  symptom.

---

## 6. What I could not verify without running the app (honesty section)

- **Whether the reverted 4-mode debug diagnostic actually left `rockNoise()`/`rockFbm()` running
  in all four modes.** Section 4.2's reconciliation depends on this; the diagnostic itself is not
  in the tree (already reverted before this document was written) and its exact code was not
  described in enough detail in the sibling doc to confirm either way.
- **Whether `u_rock_strength: 0` (or otherwise disabling `rockNoise()`) actually stops the halo
  flicker.** This is the single most decisive test available and has not been run (see Section 7).
- **Whether the flicker's character actually differs between `--view.tilt 0` and a small nonzero
  tilt**, which Section 2's finding predicts it should if any `s`/area-based mechanism were a
  meaningful contributor. Not tested live.
- **Whether the healing-pass/`effectiveS` composite-rebuild mechanism (Section 1) is observed to
  fire during an actual flicker episode.** The existing `pinStuckSince`/`pinStuckLastLogged`
  diagnostic (`tileManager.cpp`, from the elevation-shelf investigation) only fires after 3+
  continuous seconds stuck -- it would **not** catch a single-frame heal/flip event, only a
  *permanently* stuck one. There is currently no logging for a transient, one-frame attachment
  swap; correlating one with the halo flicker live would need either (a) watching for `LOGD`
  "Healed"/"Found proxy" lines (`tileManager.cpp:642-643,656-657`, Debug-build-only,
  `LOG_LEVEL=3`) around the same wall-clock moment as an observed flicker, or (b) new
  instrumentation not added here (see Section 7).
- Nothing here was confirmed by running the app, a GPU capture, or a profiler trace, per this
  project's policy -- all of the above is a static trace against the current tree.

---

## 7. Candidate next steps (not implemented -- for sign-off)

**A. Zero out `u_rock_strength` live via the GUI slider (already exposed, no rebuild needed) and pan over the same spot.**
`u_rock_strength` already has a `gui_variables` slider entry (`stylus-osm.yaml`'s `application:`
block, current uncommitted diff: `label: Rock Texture Strength, min: 0, max: 1, step: 0.05`).
Dragging it to `0` at runtime multiplies `rockShade`'s deviation from `1.0` to zero
(`stylus-osm.yaml`'s color block: `rockShade = 1.0 + u_rock_strength * rockFade *
clamp(rockSignal, -1.0, 1.0)`), silencing the noise hash's contribution to the rendered color
entirely, with **zero code change and zero rebuild** -- this is the single cheapest, most
decisive test available right now. If the halo flicker disappears (or becomes dramatically
calmer) with the slider at 0 and returns when moved back up, Section 4's hypothesis is confirmed
as at least a major contributor. Effort: trivial (one slider drag). Risk: none (a live GUI value,
trivially reversible, no rebuild).

**B. If A confirms it, the actual code fix is narrowly scoped and cheap**: either (i) reduce the
`sin()` argument's magnitude further (e.g. hash `iw` through a smaller-period wrap, or use a
hash construction that doesn't route through `sin()` on a several-hundred-thousand-magnitude
argument at all -- many standard GPU noise hashes avoid `sin()` entirely for exactly this
reason, e.g. integer-hash/bit-mixing based alternatives), or (ii) reduce `u_rock_scale`'s
influence on `pBase`'s magnitude before the floor/mod, or (iii) do the floor/mod arithmetic in a
way that's provably exact at these magnitudes (float32 exactly represents integers up to 2^24;
worth double-checking `mod()`'s own internal division/floor doesn't reintroduce error at the
current 1000-period, given GLSL's `mod(x,y) = x - y*floor(x/y)` definition). **Not implemented
here** -- needs Sebastian's sign-off and, per the task's own instructions, is out of scope for
this research-only pass regardless.

**C. Test the tilt-0-vs-nonzero prediction from Section 2.** Run the same pan-by-one-pixel test
at `--view.tilt 0` and again at e.g. `--view.tilt 0.05`, same location. If the flicker's character
is indistinguishable between the two, that's further evidence against any `s`/area-driven
elevation-attachment mechanism (Section 1.2) and for Section 4's noise-hash theory operating
independently of it. If the flicker gets *worse* or qualitatively different specifically at
nonzero tilt, that would re-open Section 1.2 as a live contributor after all.

**D. If (A) does NOT calm the flicker**, that would falsify Section 4 as the (sole) explanation
and point back toward Section 1's elevation-attachment mechanisms after all, despite Section 2's
tilt-0 argument -- in that case the next step would be adding a one-frame-resolution (not
3-second-threshold) log of every `raster.tileID`/`texture` swap event in the healing loop
(`tileManager.cpp:639-658`) and the `upgradeAttachedRasters` composite swap
(`tileManager.cpp:1207-1217`), timestamped, to directly correlate swap events against observed
flicker moments -- more invasive than (A)/(C), proposed only as a fallback.

**Not recommended:** touching `assets/scenes/stylus-osm.yaml`'s `rock-texture` style or
`tangram-es/core/src/tile/tileManager.cpp`/`rasterSource.cpp` right now -- per the task's explicit
instructions, this pass is research-only, and (A)/(C) above give a same-session, zero-risk way to
narrow down which theory is right before any code changes are proposed.

---

## 8. Summary for Sebastian

- **The elevation/tile-attachment flip mechanism you asked about is real, but structurally cannot
  be triggered by lateral panning at `--view.tilt 0`** -- `View::getTileScreenArea()` returns the
  constant `FLT_MAX` for every on-screen tile whenever pitch (tilt) is exactly 0
  (`view.cpp:665`), which is exactly the condition this project's own test protocol always uses.
  Tile LOD/overzoom level (`s`) has no positional input at tilt 0, so it cannot flip from a pixel
  of pan alone. The healing-pass/composite-rebuild machinery that *would* respond to an `s` flip
  (Section 1) is real and already explicitly designed to tolerate it (one clean re-stitch per
  flip), but its actual triggers under your test conditions are network/cache timing, not the pan
  gesture itself.
- **Sitting right next to the `rock-texture` style you just fixed (`terrain_grid: 256`) is an
  already-written, not-yet-confirmed-fixed comment describing a GPU `sin()`-hash precision bug**
  in the procedural rock noise, explicitly naming "corrupting the background-adaptive text halo's
  luminance read" as a suspected consequence, with a partial mitigation (`mod(i, 1000)`) already
  in place but never verified live. This is a much closer match to "even a single screen pixel"
  than anything elevation-attachment-related, and offers a clean explanation for why your 4-mode
  rock-texture recolor test showed identical flicker regardless of mode (the noise hash likely
  wasn't disabled by any of those four modes, only its elevation coupling was).
- **Single most decisive next test, zero rebuild required:** drag the "Rock Texture Strength"
  (`u_rock_strength`) GUI slider to `0` at the same spot where you've been seeing the flicker, and
  see if it stops. If it does, we've found it. If it doesn't, we're back to Section 1's mechanisms
  despite the tilt-0 argument, and the next step is the finer-grained per-swap logging in Section
  7(D).

```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
```
(Zermatt/Matterhorn, used throughout this thread. `--view.lat 49.38 --view.lng -123.20 --view.zoom 13`,
Lions/Cypress BC, is the other coordinate used in prior sessions if a second location is wanted.)

(Working directory: `/home/sebastian/projects/maps` -- no extra worktrees in play for this task.)
