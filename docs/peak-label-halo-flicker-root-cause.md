# Peak-label halo flicker/corruption: root cause and fix

## Summary

Peak labels (icon + name + elevation, drawn via the built-in `points` style) showed a
background-adaptive halo that was missing, corrupted, or trailing behind the camera during
panning/tilting. Other labels (plain text, or labels drawn via explicitly-styled point layers
like `poi-points`) were unaffected.

**Root cause:** `Scene::render()`'s background-halo blit (which snapshots the composited frame
for `sdf.fs`'s per-pixel luminance-adaptive halo, `docs/label-placement-plan.md` Phase 4) only
triggered on `style->type() == StyleType::text`. `PointStyle` (the built-in `points` style) is
`StyleType::point`, but it also owns and unconditionally draws its own internal companion
`TextStyle` for its linked labels every frame. Since the peak draw rule never overrides
`style:`, peak labels render through this generic `points` style rather than through
`poi-points`/`track-markers`/`loc-points` (which the blit condition happened to draw after,
since those names alphabetically sort after `"text"`).

`PointStyle` and `TextStyle` both default to `Blending::overlay` (the same value), so
`Style::compare()`'s sort falls through to its last tiebreak: alphabetical style name.
`"points" < "text"`, so `points` (and its internal label draw) always ran *before* the frame's
background blit — meaning peak labels' halo always sampled the **previous frame's** composited
image, not the current one. Since the background texture is a single, persistent GL object
reused every frame (only its contents are re-blitted), this was a permanent, structural
one-frame staleness, not transient noise — which is exactly why it looked like "ghosting"/
"inertia" trailing the pan direction, and why it never went away no matter how long the camera
sat still (a static camera still redraws GUI-only frames, e.g. cursor blink, which re-run the
same stale-sample sequence).

## The fix

- `tangram-es/core/src/style/style.h`: added `virtual bool hasLinkedTextStyle() const { return
  false; }` to `Style`.
- `tangram-es/core/src/style/pointStyle.h`: overridden to return `true` (since
  `PointStyle::onBeginDrawFrame()` always calls its own `m_textStyle->onBeginDrawFrame()`).
- `tangram-es/core/src/scene/scene.cpp`: `Scene::render()`'s blit-trigger condition is now
  `style->type() == StyleType::text || style->hasLinkedTextStyle()`, so the blit runs before
  *any* style that will sample `u_background_tex` this frame, not just styles whose own
  `type()` happens to be `StyleType::text`.

No other style class embeds a companion `TextStyle` (`ContourTextStyle`/`DebugTextStyle` are
themselves `StyleType::text`, already covered), so this fix is complete.

## What this wasn't

Several earlier hypotheses were investigated and ruled out along the way — camera-position
float precision loss, `GL_DITHER`, SDF atlas stroke-radius overrun, rock-texture noise-hash
coordinate stability, elevation-composite target-zoom flapping, and a label collision-priority
tiebreak ordering by raw (jittery) NDC depth. None of these were the cause of the reported bug;
none of the associated speculative changes are still present in the tree. Bisecting across the
feature's original commit history (back to before IBM Plex Sans/the background-halo feature
existed at all) also didn't find an earlier "good" state, because this bug was present from the
very first working version of the background-adaptive halo — it was never a regression from any
later change.

## Also fixed along the way (real, independent bugs found during this investigation)

- **Tile-bounds debug toggle didn't work live**: `DebugStyle`/`DebugTextStyle` were only
  constructed if `SceneOptions::debugStyles` was true at scene-load time, so toggling the
  "Tile bounds" checkbox afterward had nothing to act on. Fixed by always constructing them
  (their own builders already gate mesh generation on the live flag) and clearing the tile mesh
  cache (`TileManager::clearTileSets()`) when the checkbox is toggled, so already-loaded tiles
  pick up the flag without a full scene reload.
- **Render/update pairing gap**: `MapsApp::drawFrame()` could reach `map->render()` without a
  paired `map->update()` in the same tick (camera mutated directly via a gesture handler, or a
  GUI-only redraw), leaving labels' screen positions briefly stale relative to that frame's
  terrain/hillshade draw. Fixed by forcing an `update()` whenever `Map::isViewDirty()` is true
  or a GUI-only redraw is about to render.
