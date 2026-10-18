# Label typography and path labels (2026-10-16)

Goal: Swisstopo / Imhof-grade labels. One typeface (IBM Plex Sans), used in two widths,
with weight, case, tracking, colour and *path* doing the work that a mixed bag of families
used to do. This documents what was there, what the end state is, what was built, and every
knob.

## What was there before

- Fonts: everything was `IBM Plex Sans` regular/italic/semibold, plus Serif for a few names
  and Mono for peak elevations. No Condensed, no Light/Medium/Bold. Sans files were loaded
  from `/home/sebastian/.fonts/...` (not portable).
- **No tracking at all.** The text pipeline (`FontContext`, fontstash on Linux) had no
  letter-spacing parameter, so "tracked caps" were impossible.
- **Path labels were straight chords.** An elongated lake/park got a 2-point line along its
  principal axis (`addCurvedPolygonLabel`), so a banana-shaped lake or a bent glacier valley
  got a label sticking out of the shape. Lines (rivers, valleys) were used as digitized, so
  glyphs jittered along zig-zag geometry.
- **Mountain ranges were never labelled.** The tiles carry `natural=mountain_range`
  (LineString / MultiLineString / Polygon, named, from z8) and `natural=valley|gorge`
  (named lines), but only valleys had a draw rule (9.5px italic, z12+).
- Peaks were one size/weight regardless of importance; elevations used Mono.
- Colours: peaks black, water indigo, parks `darkgreen`, valleys black — nothing tied to the
  warm ground the labels sit on.

## End state (the style guide)

| Feature | Family / weight | Case, tracking | Colour | Path |
|---|---|---|---|---|
| **Mountain ranges** | Sans 300, 13–26px by zoom | UPPERCASE, 0.42 em (fit up to 0.9) | umber `range_name` | spine, smoothed 48px |
| **Valleys / gorges** | Sans 400 (gorge 500), 11–13px | mixed case, 0.22 em | olive-umber `valley_name` | line, smoothed 36px |
| **Peaks** | **Condensed** 600/500/400 by *prominence*, 13/11.5/10.5px | sentence case, 0.03 em | ink `peak_name` | point |
| Peak elevation | Condensed 400, 9px | 0.06 em | lighter umber `peak_elevation` | point |
| **Lakes** | Sans 400 *italic*, 10.5–18px by area | 0.05 em, **fit to 72% of the water's spine** | blue `water_name` | polygon spine |
| **Rivers** | Sans 400 italic, 10–14px | 0.08 em | blue | line, smoothed 18px |
| **Glaciers** | Sans 300 *italic*, 11–15px by area | 0.2 em | ice-blue `glacier_name` | point (see limits) |
| **Parks / reserves** | Sans 500, 10–12px | UPPERCASE, 0.14 em, **fit to 80%** | green `park_name` | polygon spine, smoothed 24px |
| **National parks** | Sans 600, 10.5–14px | UPPERCASE, 0.2 em | green | polygon spine |
| Countries / continents | Sans 300 | UPPERCASE, 0.34 / 0.5 em | place ink | point |
| Regions | Sans 400 | UPPERCASE, 0.24 em | place ink | point |
| Cities | Sans 600 (700 if ≥ 500k) | – | place ink | point |
| Towns / villages | Sans 500 / 400 | – | place ink | point |
| Hamlets | **Condensed** 400, 8–10px | 0.04 em | softer ink `hamlet_name` | point |
| Neighbourhoods | Sans 500 | UPPERCASE, 0.16 em, *upright* | hood brown | point |
| Roads | **Condensed** 400 (500 for trunk–secondary) | 0.03 em | grey | line |
| Islands | Sans 400 italic | 0.12 em, blue | water blue | point |

Rules behind the table:

- **Italic only for water and ice** (Imhof). It was previously also on neighbourhoods,
  valleys and parks; those are upright now.
- **Tracked capitals for things that are big and diffuse** (ranges, regions, countries,
  parks): tracking makes a name read as *extent*. **Tight sentence case for things small and
  precise** (peaks, hamlets, elevations).
- **Condensed where space is the constraint**: peaks (collision footprint, crowded terrain),
  roads (name must fit a curve), hamlets.
- **Weight is hierarchy**: 300 for big and quiet (ranges, countries, glaciers), 400–500 body,
  600–700 for importance (big cities, major peaks).
- **Peak hierarchy comes from the real OSM `prominence` tag only** (≥ 900 m semibold 13px,
  ≥ 350 m or a Wikipedia entry medium 11.5px, rest regular 10.5px). Elevation alone never
  makes a label heavier (same rule as for label selection).
- **Colour is warm umber on terrain** (the ground under the type is warm grey-beige rock and
  scree), blue for water, ice-blue for glaciers, green for protected land. Lightness steps
  carry the order: peak names darkest, elevations lighter, ranges/valleys lighter again and
  relying on size/tracking. Dark scene (`stylus-osm-dark.yaml`) overrides every new token.
- Range names outrank peaks only while zoomed out (priority 13.5 below z11, 14.9 from z11),
  so a range name never punches holes in a view whose point is its peaks.

## Engine work (tangram-es)

All new YAML keys work in `draw:` rules (`font: letter_spacing`) and inside `points: text:`
(`text: font: letter_spacing`); `text2:` (the peak elevation) has its own
`text2: font: letter_spacing` and `text2: font: fill`.

| Key | Meaning |
|---|---|
| `font: letter_spacing: <em>` | Tracking. `0.1` = 10% of the font size between every pair of letters. Stops OK. |
| `font: letter_spacing_fit: <0..1>` | Polygon labels only: track out so the label spans this fraction of the polygon's spine (lake) or horizontal chord (compact polygon). Never tighter than `letter_spacing`. |
| `font: letter_spacing_max: <em>` | Cap for the fitted tracking (default 0.6). |
| `smooth: <px>` | Gaussian smoothing radius for the label path (lines; polygon spines). |
| `text2: font: fill` / `letter_spacing` | Secondary (elevation) label colour / tracking. |

Numeric JS functions are now accepted for `font: size`, `letter_spacing*` and `smooth`
(e.g. lake size by area, peak size by prominence).

Changes:

- `text/fontContext_stb.cpp`: tracking via fontstash's existing per-iterator `spacing`
  (`layoutLine`, `layoutMultiline`). The harfbuzz/alfons path (`FontContext::layoutText`,
  `LineLayout::addTracking`) is implemented too, but unused in this build
  (`-DFONTCONTEXT_STB`).
- `util/labelPath.{h,cpp}` (new): `smoothPolyline` (arc-length resample, Gaussian with
  point-reflection endpoints so paths never shrink, Douglas-Peucker afterwards),
  `trimPolyline`, `pointInPolygon`, and `polygonCenterline`: slices a polygon (outer ring +
  holes, even-odd) perpendicular to its principal axis, takes the middle of the interval that
  continues the previous point (so an island or pinch doesn't make the spine jump), keeps the
  longest stretch at least text-height wide, smooths it, and verifies it stays inside.
- `style/textStyleBuilder.cpp`: `addCurvedPolygonLabel` now sets a *single* centered label
  along that spine instead of a straight chord (polygons with holes included);
  `prepareTrackedLabel` computes fit-tracking exactly by measuring the text's width at two
  trackings (no cluster counting); line labels are smoothed before placement; a polygon
  that touches the tile edge is a fragment of something bigger and never fit-tracked (the
  neighbouring tile would pick a different tracking for the same feature).
- `text/fontContext_stb.cpp`: one-time warning when a scene asks for a font
  family/weight/style it never declared (it silently rendered in the default font before).
- Tests: `tests/unit/labelPathTests.cpp` (smoothing, trimming, banana-shaped lake, narrow
  rectangle, island). 2343 assertions pass.

Fonts: 15 Plex files bundled in `assets/fonts/plex/` (OFL; Sans 300/400/500/600/700 + Light,
Regular, Medium, SemiBold italics; Condensed 300–700 + Regular italic), declared in
`stylus-osm.yaml` `fonts:` with relative paths — no more dependence on `~/.fonts`.

## Known limits / open items

- **Glacier names are still points.** The tile schema puts the name only on the glacier
  *centroid* (poi layer); the glacier polygon has `area` but no `name`/`osm_id`. Fix is
  server side (`process.lua`: write `name` onto the landuse glacier polygon). The
  `landuse_name` rule would then need `natural: glacier` added to its filter, and glaciers
  would follow their flow axis with no style change.
- Tracking `fit` only applies to polygons not touching the tile edge, and only for
  polygons. Ranges/valleys are lines clipped per tile, so they use a fixed em tracking.
- Text size follows the *tile* zoom (stops evaluate at integer tile zoom), as before.
- The label path for polygons is a slice-midline, not a true medial axis: a polygon that
  folds back on itself along its principal axis (a U-shaped fjord) gets a spine through the
  widest lobe, not around the bend.
- Everything in the table is a first calibration made without looking at the map (project
  policy); sizes, tracking, halos, and the umber/blue/green values are for Sebastian's eye.
- **Pre-existing, not touched:** `peaks` → `text: text_repeat_distance: 0` is parsed as the
  unknown key `text:text_repeat_distance` (log: `Unknown StyleParam text:text_repeat_distance:0`),
  so the round-13 repeat-group fix for peak *names* has never taken effect. The correct key
  inside a `points: text:` block is `repeat_distance`. Left alone because enabling it changes
  peak-name suppression behaviour; one-line fix if wanted.

## Round 2 (2026-10-16): the area label planner

Feedback on round 1: park names repeated 6+ times, labels spilled out of lakes/parks, lakes
got two labels, rivers were struck through by their banks, small parks were off-centre,
peaks too light, halos too thin. The common core is "label an area": where, how many lines,
how much tracking, straight or along the spine, and *never outside the shape*.

**Algorithm** (`TextStyleBuilder::addAreaLabel`, `util/labelPath.*`), used for every polygon
text rule with `letter_spacing_fit > 0` (parks, protected areas, lakes; glaciers once the
tile has a polygon name). No centroid fallback: no room means no label.

1. Rasterise the polygon (px, even-odd, holes) into a conservative `InsideMask` (<= 128^2
   cells; the outline's own cells count as outside) with a summed-area table: "is this
   rectangle entirely inside?" is O(1). Edges lying on the tile boundary are clip edges, not
   shoreline, so they are pushed outwards half a tile (otherwise a name wider than one tile
   could never be placed).
2. Horizontal candidates, 1..3 lines (greedy wrap at word boundaries only -- no hyphenation).
   For each: widest band of the text's height that is fully inside (scan over rows, O(cells));
   tracking is stretched from `letter_spacing` up to `letter_spacing_max` (em) to fill
   `letter_spacing_fit` of the run; for 2-3 lines the line spacing is stretched
   (0.15 em .. 3 em) to fill 80% of the vertical room. More than one line is considered only
   when the room is "tall" (vertical room >= 0.7 x width); the candidate whose box aspect
   best matches the region's aspect wins.
3. Path candidate for elongated shapes (elongation >= 1.8): the existing spine
   (`polygonCenterline`), clearance tested on both sides with ray casts against the mask, the
   longest run where the whole text strip fits, window slid to the *thickest* part. It wins
   if it offers >= 15% more length than the horizontal block, or if the shape is thin and the
   horizontal option needed line breaks.
4. Emit one label, verified against the mask with the real layout. The label is in a repeat
   group keyed on its text (1200 px), so the fragments of one park in neighbouring tiles
   compete and the roomiest one wins; priority gets a tiny bonus for room.
5. Busy-ness: the tile's occupancy grid (ferry lines, roads, shorelines) is sampled under each
   candidate band and penalises it.

Other round-2 changes: `path_offset: <px>` (line labels, shifts the path up/left of the
reading direction; rivers sit beside the river); peaks are 600/700 Condensed 12.5/13.5/15 px
with 11 px 600 elevations and a 2.2 px halo; lakes/rivers/glaciers/parks get darker
colours and 3-3.5 px halos; small municipal park POIs: icon above, name centred below,
upright medium dark green, shown only when the park's area (estimated in px^2) is clearly
bigger than icon + name.

**Glaciers**: `scripts/tilemaker/process.lua` now writes `name` onto glacier polygons. After
regenerating tiles set `glacier_polygon_labels: true` and `glacier_point_labels: false`
(globals in `stylus-osm.yaml`) to get glacier names as area labels. Until then glaciers keep
the centroid point label (darker, regular weight, hidden for small ice) and can still spill.
