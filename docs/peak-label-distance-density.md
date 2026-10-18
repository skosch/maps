# Peak labels: distance-aware density (2026-10-13)

Goal: standing on a summit, tilt the camera and be able to ask "what's that peak on the
horizon?" Near the camera many (even minor) peaks are labeled, at mid distance only the ones
that stand out, on the horizon only the dominant massifs.

## Why far peaks were missing

1. **No data.** The tile server emits `mountain_peak` from z11 only (`scripts/tilemaker/
   config.json`; in the cache, z7-z10 tiles hold 1-2 peaks each, z11 holds ~15 000).
   The `osm` source picks coarser tiles with distance (area-based LOD, `zoom_retention`),
   so under tilt the far half of the view was built from z8-z10 tiles: no peaks.
2. **Ranking.** `LabelManager::priorityComparator()` preferred "closer to camera" and
   "higher tile zoom" ahead of fractional priority, so in any collision a foreground knoll
   beat a horizon summit.

## What changed

| Piece | Where |
|---|---|
| Peaks come from their own source `osm-peaks` (same server, `max_zoom: 11`, large `zoom_retention`, no `rasters`, own cache file) and their own layer `peaks` | `assets/scenes/stylus-osm.yaml` |
| `pois` keeps a stub `peak:` rule that consumes peaks and draws nothing | same |
| Dominance radius: radius within which the peak is the highest DEM point, polar-grid sampled outwards, stops at the first higher sample (minor bumps cost tens of samples) | `peakDominanceRadius()`, `util/textureShading.cpp` |
| Measured once per peak in `Label::refinePriority()` (main thread, existing 2 ms/frame budget); retried up to 8 frames if neighbouring DEM tiles are missing | `labels/label.cpp` |
| Gate: peak (icon + its name + its elevation, via `gateOwner()`) shown while `dominance >= peak_dominance_gate * distance` (15% hysteresis). Hiding goes through `occlude()` so labels fade | `LabelManager::processLabelUpdate/handleOcclusions` |
| Collision rank among gated peaks: `dominance / distance`, log-bucketed, within the named/unnamed half-tier | `LabelManager::priorityComparator()` |
| Raster-less tiles accept DEM up to 7 levels coarser than the tile for label elevation (far peaks must not sit at sea level) | `processLabelUpdate()` |

2026-10-16 follow-ups: the gate is `dominance >= ratio * d * d / (d + 10 km)` (lenient up close);
collision rank is the peak's dominance; peaks skip build-time collision and a coarse tile
(styled below z13) hands over at most its best 100; the source's styling zoom is quantized
(data zoom / +2) so tiles are not rebuilt on every zoom change; dominance results are cached
by position and sampled with a per-call tile cache (max radius 12 km).

Tunables (scene globals): `peak_dominance_gate` (0.08), `peak_show_unnamed` (false),
`peak_candidacy_ele_floor` (500, unchanged).

0.08 means "highest point within a radius of 8% of the viewing distance": a 200 m knoll shows
out to 2.5 km, a 2 km-dominant summit out to 25 km, a 9 km-dominant one (Matterhorn) out to
~110 km.

## Unrelated tweaks made in the same pass

- Name/elevation gap: `anchor_gap_scale` 0.25 -> 0 (and text2 now inherits it instead of a
  literal 0.25f).
- Name prefers above, elevation below: `Options::anchorVerticalPref`, +/-0.06 cost in
  `refineAnchor()` - smaller than a ridge-vs-flank difference, so ridge avoidance still wins.
- Halo (`sdf.fs`): luminance fade 0.35..0.75 -> 0.06..0.20; peak label stroke 3 -> 1.5 px.
- Unnamed elevation points hidden by default.

## Known limits

- Peaks appear only while the view zoom is >= 11 (the `osm-peaks` tile zoom is capped by the
  view's LOD zoom like any other source). Zoomed way out there are still no peak labels.
- Dominance is measured on whatever DEM is resident; a peak at the edge of the loaded area
  can read as more dominant than it is. Sampling is ~15% of the radius, so a narrow taller
  summit can be missed.
- `osm-peaks` tiles are separate downloads/cache (`stylus-osm-peaks.mbtiles`), full z11
  tiles (all layers; the anchor-occupancy grid needs them).
