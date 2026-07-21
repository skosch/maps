# Rock-texture `terrain_grid` fix: label-flicker side-effect investigation

**Status (2026-07-20):** research only, no fixes implemented, per the established "research
first, present plan, implement only after sign-off" workflow (same as the three terrain-drape
docs this one is modeled on). Nothing in `assets/scenes/stylus-osm.yaml`'s `rock-texture` style
was touched — that fix is confirmed correct and out of scope here. No diagnostic instrumentation
was added to the tree either (see §5 for what's proposed but not applied, and why).

**Headline finding:** the flicker is real, has a concrete and traceable mechanism, and is best
understood as **an existing (previously milder) settling-latency behavior in the peak-label
texture-shading refinement system, now stretched longer by a genuinely new CPU cost that
`rock-texture`'s correctness fix added to the exact same shared worker thread pool that also
processes the elevation-raster tiles that refinement depends on.** It is not a new class of bug —
it is the same "provisional priority/anchor until the elevation mosaic is ready" behavior the
label-placement work already knew about and budgeted for (round 8, round 15), just visible for
longer because tile builds over `bare_rock` got slower and now compete harder for the same
limited worker threads that elevation tiles also need. See §4 for the honest caveat on how
big "longer" really is without a real trace.

---

## 1. Confirmed facts, with citations

### 1.1 The tile-build cost `rock-texture`'s fix newly turns on is real and already measured (for the *general* case)

`docs/tile-pipeline-perf-plan.md` §2.1 measured `buildPolygonGrid` (the terrain-grid
tessellation routine, `tangram-es/core/src/util/builders.cpp`) at **26–49% of all vector-tile
build CPU** in release builds (worse in debug), a **10.4x triangle amplification** (177k earcut
triangles → 1.84M output triangles in one z11 session), and **3D terrain triples-to-quadruples**
the median vector-tile build cost, with grid tessellation responsible for roughly half of that
multiplier. That measurement predates the `rock-texture` fix and was taken against styles that
already had `terrain_grid` set correctly (chiefly `unlit-polygons`). `rock-texture` was
confirmed (per the background given for this task) to have silently defaulted to `gridRes = 0`
for its entire prior lifetime — i.e. it paid **none** of this cost before, and now pays the
**same class of cost** every other terrain-grid style already pays, specifically for `bare_rock`
features.

`rock-texture`'s current value, `terrain_grid: 256` (`assets/scenes/stylus-osm.yaml:684`),
matches `unlit-polygons`' *original* value and the code comment there confirms the reasoning
(overzoom up to 2 levels past the OSM source's `max_zoom: 14`, `64 << 2 = 256`). One relevant
wrinkle found while reading the same file: `unlit-polygons` itself is currently at
`terrain_grid: 1024` (`stylus-osm.yaml:665`), flagged in its own comment as a "TEMP EXPERIMENT
(Matterhorn drape investigation, round 7) — was 256." `docs/terrain-drape-resolution-plan.md`'s
intro says this specific 256→1024 bump was "since fully reverted," but the live scene file still
shows 1024, and `docs/terrain-drape-cellcaps-investigation.md` (dated 2026-07-19, i.e. newer)
explicitly says its own trace was done "with the round-7 TEMP experiment constants still in
place." **This means `unlit-polygons` — every other land-cover polygon (forest, residential,
scree, etc.) — is currently tessellating at 4x the linear resolution (16x the cells) of
`rock-texture`'s 256, and was already the single largest per-tile CPU cost before this task's fix
even shipped.** This is a pre-existing, unrelated loose end (likely relevant to whatever the
concurrent "stuck shelf" investigation is looking at, not this one) — noted here only because it
matters for calibrating "how much of a big deal is `rock-texture`'s new 256 cost relative to
what's already normal in this pipeline." Answer: real, but not obviously the biggest single
polygon-tessellation cost on a typical mountain tile — `unlit-polygons` on the same tile's other
land-cover, if present, is likely costing considerably more already, unremarked-on by Sebastian.

Also confirmed: **R1.1 from the perf plan (arithmetic lattice indexing instead of hashing every
interior grid corner) is already implemented** — `buildPolygonGrid` (`builders.cpp:157-176`) has
`emitLatticeVertex`/`latticeIndex` doing exactly this, with a comment citing
"tile-pipeline-perf-plan.md R1.1." **R1.2 (adaptive/coarsened resolution for oversized polygons)
is NOT implemented** — no cap on cells-per-polygon exists anywhere in `builders.cpp`. This
matters directly for `rock-texture`: the task background's ~14.5 km² real-world `bare_rock`
example is precisely the case R1.2 was designed for and it gets none of that mitigation today —
every `bare_rock` tile touching that feature pays the full 256-res tessellation, unmitigated,
including the multi-chunk `chunkOffsets`/`flushChunkIfNeeded` path (`builders.cpp:182-196`) for
any single tile-local piece whose vertex count nears the `uint16` index ceiling.

### 1.2 Vector tile builds and elevation raster-tile processing share one worker thread pool and one priority queue — this is the concrete contention mechanism

- `TileWorker::run()` (`tangram-es/core/src/tile/tileWorker.cpp:35-100`) has a **single**
  `m_queue` of `TileTask`s, popped by `std::min_element` on `getPriority()`/proxy-state/source
  generation (lines 74-84), with **no distinction anywhere in the comparator between a vector
  tile task and a raster tile task** — both are plain `TileTask` (or `BinaryTileTask`) subclasses
  competing on the same footing.
- `RasterTileTask::process()` (`tangram-es/core/src/data/rasterSource.cpp:493-540`) — LERC decode
  (`source->createTexture`) plus, for non-subtask rasters, a full `_tileBuilder.build()` call —
  runs through **the exact same** `task->process(*builder)` call site inside
  `TileWorker::run()` (`tileWorker.cpp:94`) as an OSM vector tile's `TileTask::process()`
  (`tile/tileTask.cpp:34-46`, which does the MVT parse + `_tileBuilder.build()` that invokes
  `PolygonStyleBuilder`/`buildPolygonGrid` for `rock-texture`).
- Both task types are also fed into the worker queue via the **same** generic callback,
  `TileManager::m_dataCallback` (`tile/tileManager.cpp:139-151`, `m_workers.enqueue(task)`),
  regardless of which `TileSource` produced them.
- Task priority (`TileTask::setPriority`, `tile/tileManager.cpp:659`,
  `glm::length2(tileCenter - _view.center) * scaleDiv`) is purely geometric distance-to-viewport-
  center — **it has no source-type awareness**, so an elevation raster tile and an OSM vector
  tile covering the same screen area get essentially the same priority and compete evenly for
  worker slots, not "raster always wins" or "raster always loses."
- Worker count (`app/src/mapsapp.cpp:812-813`) is
  `defaultTileWorkers = min(6, max(2, hardware_concurrency - 2))`, overridable via
  `tangram.num_tile_workers`. This is *higher* than the `docs/tile-pipeline-perf-plan.md`
  snapshot's stated default of 2 (that doc is 8+ days old per its own memory-staleness warning;
  R3 from that plan — "raise `num_tile_workers` above 2" — appears to have since been
  implemented). This reduces, but does not eliminate, the contention risk: it is still a small,
  fixed-size pool shared by every tile source, and a burst of newly-visible mountain tiles (both
  `bare_rock`-bearing OSM tiles and their corresponding elevation raster tiles) can still exceed
  it during the first few seconds after panning/zooming into a mountain view.

**Conclusion of 1.1 + 1.2**: a `bare_rock` vector tile build that now takes measurably longer (own
earcut + up to 256-res grid tessellation, unmitigated for oversized features) occupies one of a
small, fixed pool of worker threads for that much longer, and that same pool is what an elevation
raster tile (LERC decode) for the *same screen area* needs in order to become available. This is
a real, direct, code-confirmed contention path — not a stretch.

### 1.3 Peak-label priority/anchor refinement is gated on the elevation mosaic being fully stitched and resident — and only runs for peak labels

- `Label::refinePriority()` (`tangram-es/core/src/labels/label.cpp:97-165`) and
  `Label::refineAnchor()` (`label.cpp:167-265`) both bail out (`return false`, meaning "retry next
  frame") unless `elevMgr.m_elevationSource` is set AND (via `sampleTextureShading()`,
  `util/textureShading.cpp:201-241`) `elevationSource.getExistingMosaic(raster.tileID)` returns a
  **non-null, already-stitched** mosaic texture with buffer data. `getExistingMosaic()` is
  explicitly documented as "cheap reuse-only... never triggers a new mosaic build" — it just
  checks whether the raster pipeline has already finished for this tile.
- `LabelManager::processLabelUpdate()` (`labelManager.cpp:121-174`) calls these every frame,
  main-thread only, for any label with `m_prominenceRefined`/`m_anchorRefined` not yet true,
  gated additionally by a **wall-clock anchor-refinement budget**
  (`kAnchorRefineBudgetMs = 4.0`, `labelManager.cpp:41`, added in Phase 7 round 15) — once a
  frame's 4ms budget for `refineAnchor()` calls is spent, remaining labels wait for a later frame
  (`m_needUpdate = true`, `labelManager.cpp:172`).
- **This refinement path is opt-in and, today, exclusive to peak labels**: `priority_texture_shading:
  true` appears exactly once in `assets/scenes/stylus-osm.yaml` (line 2258, the peak draw rule),
  and `text:anchor_texture_shading: true` appears exactly once (line 2349, same rule's name-label
  block). No other draw rule in the scene opts into either flag.
- Until refinement succeeds, a peak label sits at its **tile-build-time provisional priority**
  (the `priority:` JS function in the peak draw rule, `stylus-osm.yaml:2244-2257` — a real,
  reasonable value derived from OSM `prominence`/`ele` tags, not a placeholder) and its
  **default anchor order** (the classic 8-position Yoeli/Imhof fallback order,
  `textStyleBuilder.cpp:773-777`, itself already re-sorted once at tile-build time by vector-
  proximity cost via `salienceOrderedAnchors()`). Once refinement succeeds, both are
  **fully re-sorted/replaced in one shot** (`refineAnchor()`'s comment at `label.cpp:246-251`
  explicitly notes this "only ever runs once per label" — no gradual/damped transition,
  by design, since repeat runs aren't expected). That one-shot jump from provisional to
  final priority/anchor **is** what would read as "flicker" or "settling" to an observer: a
  label (or a collision winner/loser pair of labels) can visibly change position, or a
  previously-losing peak can suddenly out-rank a neighbor, the instant refinement completes.

### 1.4 This settling-latency behavior already existed and was already known to be visible on cold cache — independent of the `rock-texture` fix

Phase 7 round 15 (`docs/label-placement-plan.md:1739-1792`, dated 2026-07-18 — i.e. this is not
retroactive rationalization, it was written before Sebastian's flicker report) directly measured,
at the Zermatt/Matterhorn coordinates on a cold cache: **a single frame recorded
`anchorRefineTimeUsedMs = 165.89` (41x the 4ms budget)**, attributed to building the
texture-shading pyramid from scratch for a newly-seen mosaic
(`sampleTextureShading()`'s own comment, `textureShading.cpp:213-227`, on why this is a one-time,
non-preemptible cost per mosaic). That round's own conclusion: "this is expected, not a
regression... the dominant per-call cost on a COLD mosaic... is a cost this budget change doesn't
touch." **This establishes that some degree of multi-frame (and likely multi-hundred-ms-scale)
settling delay for peak labels on first visit to a mountain area was already a known, accepted
behavior before this task's fix shipped.**

What `rock-texture`'s fix adds is a **new, additional** source of delay further upstream in the
same causal chain: not the pyramid-build cost itself (§1.3's item is main-thread, downstream of
the mosaic being stitched, and is untouched by anything in `builders.cpp`), but the time it takes
for the elevation raster tiles that feed that mosaic to even finish processing on the shared
worker pool (§1.2), because that pool is now also busy with slower `bare_rock` vector-tile
builds it wasn't busy with before.

---

## 2. What I could not verify without running the app

- **Actual magnitude of the added delay.** Everything in §1 is a structurally-confirmed
  mechanism, but I have no real trace of `bare_rock` tile-build durations post-fix, nor of how
  long elevation raster tasks now sit queued behind them during a real pan/zoom into the
  Zermatt/Matterhorn area. The `tile-pipeline-perf-plan.md` numbers (12.4–47ms avg, up to 357ms
  max per tile) are for tiles that mostly did NOT include a large newly-tessellated `bare_rock`
  feature; the 14.5 km² multi-tile example in this task's background is plausibly considerably
  worse per-tile than those averages, but I have no direct measurement of it.
- **Whether the flicker is dominated by §1.2's contention (a *new* delay) or by §1.4's pre-existing
  cold-mosaic-pyramid cost (an *old*, already-known delay that would happen even without this
  fix).** Both are real and additive in principle, but I can't apportion how much of what
  Sebastian is seeing is "worse than before" versus "the same known cold-cache cost, just now
  more noticeable because he specifically re-tested the exact area right after the fix, with a
  cold cache, immediately after confirming the fix worked visually" (which the task's background
  describes almost exactly).
- **Whether frame pacing/rendering itself hitches** (a distinct concern from label priority
  changing) — e.g. a genuinely large single-tile mesh upload (the multi-chunk `bare_rock` case)
  landing on the main thread as one frame's VBO upload. `docs/tile-pipeline-perf-plan.md:88` notes
  "Main thread: task completion, mesh/label finalization, VBO uploads" happens per tile, but
  there is no measurement anywhere in the docs of VBO upload cost specifically (only texture
  upload costs were profiled). I could not rule this in or out; see Candidate C in §3.
- Nothing here was confirmed by looking at an actual profiler trace or the live app, per this
  project's policy — Sebastian generates all visual/live evidence himself.

---

## 3. Candidate causes, ranked by confidence

1. **(Highest confidence) Worker-pool contention delaying elevation-mosaic availability, stretching
   the already-known provisional→final settling window for peak labels.** Directly supported by
   §1.2 (confirmed shared queue/pool) + §1.3 (confirmed hard dependency of label refinement on
   mosaic availability) + §1.4 (confirmed this settling window was already visible/known even
   without this fix, so making the upstream raster-availability step slower plausibly stretches
   it further). This is consistent with Sebastian's report being specific to mountainous/
   `bare_rock`-heavy areas: that's exactly where (a) the new tile-build cost exists and (b) peak
   labels — the only labels using this refinement path — exist. The two conditions co-locate by
   nature of what a "peak" and "bare rock" both are, not by coincidence.
2. **(Medium confidence) Pure exaggeration of a pre-existing, already-accepted cost, not really a
   new problem worth fixing.** §1.4's round-15 finding already shows a single frame with an
   uncontested 165ms pyramid-build cost is normal on cold cache. It's plausible the marginal
   difference from `bare_rock`'s new tessellation cost is small relative to that already-large
   number, and what Sebastian is noticing is mostly the pre-existing behavior, freshly observed
   because he happened to retest the identical cold-cache scenario right after the fix. I can't
   rule this in or out (§2) — it's the honest alternative to #1, not a strictly worse theory.
3. **(Lower confidence) A genuine main-thread frame hitch from very large single-tile mesh
   uploads**, independent of the label system entirely — i.e., what reads as "labels flicker"
   might partly be the whole frame stuttering (labels included) rather than labels specifically
   changing position/priority. Plausible given the multi-chunk `chunkOffsets` path exists
   specifically for oversized features, but entirely unmeasured (§2) — no data either way.
4. **(Low confidence, worth naming so it isn't force-fitted in) A general, non-`bare_rock`-specific
   worker-contention effect.** The underlying mechanism (§1.2) is generic infrastructure, not
   actually tied to `bare_rock`/`natural` tags — any sufficiently expensive tile build of any
   style could, in principle, delay any other queued raster task the same way (e.g. the
   `unlit-polygons` 1024-res TEMP experiment from §1.1 is arguably an even bigger version of the
   same generic problem). The reason the *symptom* looks `bare_rock`/mountain-specific is that
   (a) peak labels are the only labels using texture-shading refinement at all today (§1.3) and
   (b) `bare_rock`'s fix is the only *recent* change to a style's tessellation cost — not because
   the contention mechanism itself has any special affinity for `bare_rock`. Flagging this per
   the task's request: if a future report describes flicker/settling delay for some *other*
   label type that also opts into texture-shading refinement, on a tile with some *other*
   unusually expensive style, this same mechanism would explain it — it is general, just not
   currently observable anywhere else because nothing else opts into refinement.

---

## 4. Is this an acceptable cost of the correctness fix, or a distinct fixable bug?

**My best-confidence read: acceptable-but-improvable.** The `rock-texture` fix is correct and
necessary (per the task background, already confirmed working) — `terrain_grid: 256` is not
excessive on its own (it matches `unlit-polygons`' original, pre-experiment value) and paying real
tessellation cost for real drape accuracy on `bare_rock` is the whole point of the fix. This is
not "a bug in the fix." But the flicker's *specific* mechanism (§3.1) is not something the fix
had to accept as-is — the settling-latency system it's stretching (§1.3/§1.4) already has one
unmitigated cost (R1.2's oversized-polygon adaptive resolution, §1.1) that would directly reduce
how much *extra* work `bare_rock` tiles impose on the shared worker pool for exactly the large-
feature case in this task's background, without touching drape correctness for normal-sized
`bare_rock` patches (small ones already fit in one or two grid cells and pay almost nothing).

## 5. Candidate fix directions, with tradeoffs (not implemented — for sign-off)

Ranked in the same rough order as §3's confidence, i.e. most-targeted-at-the-likely-real-cause
first:

**A. Implement R1.2 from `docs/tile-pipeline-perf-plan.md`** (adaptive/coarsened grid resolution
for polygons whose bbox spans more than N cells) — this was already planned and explicitly
deferred pending "measure if R1.1 isn't enough"; the `bare_rock`-fix side effect is a concrete
reason to revisit it now, specifically for large `bare_rock` features. Effort: medium (per the
perf plan's own sketch). Risk: low–medium — coarser drape fidelity on huge polygons only, and
the perf plan's own reasoning is that huge polygons are exactly where a coarser grid is least
visually noticeable while a small/typical `bare_rock` patch (the summit-undershoot bug's original
concern) is unaffected since it stays under the cell-count threshold and keeps full resolution.
This directly reduces the worker-time `bare_rock` tiles cost, which is the root of §3.1's theory,
without reverting the correctness fix.

**B. Give elevation-raster tasks a scheduling edge over vector tasks in the shared `TileWorker`
queue** (e.g. a small priority bonus, or a dedicated reserved worker slot for raster sources) —
directly targets §1.2's contention mechanism at its source rather than reducing `bare_rock`'s
cost. Effort: small–medium (the comparator in `TileWorker::run()`, `tileWorker.cpp:74-84`, already
special-cases proxy state and source generation, so adding a source-type term is a small, precedented
change). Risk: medium — could starve vector-tile visibility if tuned too aggressively (vector
tiles are what makes anything else appear at all, including the peak icon itself before its label
priority even matters), and this queue is shared across *all* tile sources project-wide, so a
change here has blast radius well beyond this one bug. Needs real profiling to tune, not a blind
constant.

**C. Decouple the visible symptom from the timing entirely: don't show a peak label at its
provisional priority/anchor at all until refinement succeeds once.** This is a design change, not
a perf fix — instead of "jump from provisional to final," peaks would simply pop in later
(once refined) rather than visibly resettle. Effort: small (a state check in
`LabelManager`/`Label` gating visibility on `m_prominenceRefined && m_anchorRefined` for labels
that opted into either flag). Risk: low technically, but a real UX tradeoff Sebastian needs to
judge, not self-grade: replaces "flicker" with "delayed pop-in," which may or may not read as
better, and changes behavior even in the *already-accepted* cold-cache-pyramid-cost case from
§1.4, not just the new `bare_rock` contention case. Only pursue if A/B don't move the needle
enough and Sebastian prefers pop-in-late over resettle-visibly.

**D. Do nothing beyond A.** If a diagnostic (§6) shows the worker-queue delay §1.2 adds is small
relative to the already-accepted §1.4 pyramid-build cost, the honest conclusion is that this is
mostly perceptual/timing coincidence (candidate #2 in §3) rather than a new problem, and A alone
(a good idea anyway, already on the books) is sufficient.

**Not recommended:** reverting `terrain_grid` on `rock-texture` — this was the actual bug fix and
is out of scope/explicitly excluded by this task.

---

## 6. Proposed cheap diagnostic (described only — NOT added to the tree)

Mirroring the `LOGD`-based, temporary-and-reverted instrumentation style used throughout
`docs/tile-pipeline-perf-plan.md` and the terrain-drape docs, to directly measure §3's competing
theories rather than guess further:

1. **Per-tile build duration, tagged by whether the tile's build included a `rock-texture` mesh.**
   In `TileTask::process()` (`tile/tileTask.cpp:34-46`) or the `PROFILE_SCOPE` call site around it
   in `TileWorker::run()` (`tileWorker.cpp:92-96`), add a `TEMP DIAGNOSTIC` `LOGD` around the
   `_tileBuilder.build(...)` call logging: source name, tile ID, wall-clock duration
   (`std::chrono::steady_clock`), and whether the built tile's mesh set includes a `rock-texture`
   entry (checkable post-build via the tile's style mesh map). This directly answers "how
   expensive are real `bare_rock` tiles now, in the wild, at the Zermatt coordinates" instead of
   extrapolating from the pre-fix `unlit-polygons`-only numbers in §1.1.
2. **Elevation raster-task queue wait time.** In `RasterTileTask::process()`
   (`data/rasterSource.cpp:517`), log a timestamp at task creation (constructor,
   `rasterSource.cpp:502-504`) and another right at the top of `process()`; the delta is time
   spent queued/waiting for a worker, not doing useful work. Cross-reference against
   diagnostic 1's concurrent `rock-texture` tile builds (same wall-clock window) to see whether
   raster-task wait time visibly spikes specifically when `bare_rock` tile builds are in flight
   on the same pool — this is the direct falsifiable test of §3.1 vs §3.4.
3. **Time-to-first-refinement per peak label.** In `LabelManager::processLabelUpdate()`
   (`labelManager.cpp:127-138` and `151-174`), log (once per label, guarded by a `bool` so it
   fires only on the frame `m_prominenceRefined`/`m_anchorRefined` FIRST flips true) the
   wall-clock time since that label's owning tile was first requested/visible. This is the
   actual end-to-end number Sebastian is perceiving as "flicker duration" — comparing it
   before/after a hypothetical fix (or just observing its absolute magnitude) is the most direct
   possible confirmation, more informative than either of the above in isolation.

None of these three were added to the tree. They're small, mechanically safe (`LOGD` only,
compiles out at release `LOG_LEVEL`, no behavior change), and match the existing project
convention closely enough that adding them is a reasonable next step — but per this task's
instructions and this project's "research first, present plan, implement only after sign-off"
workflow (which the three terrain-drape docs this one mirrors followed strictly), they're
described here for Sebastian's sign-off rather than applied speculatively.

---

## 7. Summary for Sebastian

- **Best-confidence explanation**: the flicker is the peak-label texture-shading refinement
  system's known "provisional priority/anchor until the elevation mosaic loads" behavior
  (already documented, already visible on cold cache per round 15), now stretched by real
  contention: `bare_rock` vector-tile builds and elevation raster-tile processing share one
  small worker thread pool and one priority queue (confirmed in code, §1.2), and `bare_rock`
  tiles are now doing real, uncapped tessellation work they weren't doing before (confirmed,
  §1.1) — including zero mitigation for the huge multi-tile case from this task's background,
  since the perf plan's own adaptive-resolution idea (R1.2) was never implemented.
- **Acceptable cost vs. distinct bug**: leans "acceptable side effect of a correct fix, but with
  one concrete, already-planned, low-risk improvement available (R1.2)" rather than "a new bug
  needing an urgent scheduling fix." I did not find evidence this is a *new class* of problem —
  it's the same mechanism the label-placement work already knew about, just fed a slower upstream
  input.
- **What to check next**: run the §6 diagnostics (or just #3 alone, if minimal) at the Zermatt
  coordinates on a genuinely cold cache, and compare time-to-first-refinement against round 15's
  own 165ms cold-pyramid baseline — if it's dramatically larger than that baseline, §3.1
  (worker contention) is confirmed as the dominant additional cause and A/B in §5 are worth
  doing; if it's roughly the same order of magnitude, this is mostly candidate #2 (pre-existing
  cost, freshly noticed) and A alone (already a good idea) is enough.

```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
```

(Working directory: `/home/sebastian/projects/maps` — no extra worktrees in play for this task.)
