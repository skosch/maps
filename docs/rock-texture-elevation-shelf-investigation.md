# Rock-texture "elevation shelf" investigation — a permanently-stuck coarse elevation raster,
# structurally distinct from the just-fixed `terrain_grid: 0` mesh-density bug

**Status (2026-07-20): research only, no fix implemented**, per the established "research first,
present plan, implement only after sign-off" workflow. One TEMP DIAGNOSTIC (a logging-only code
change, no behavior change) has been added and is described in §5 — everything else in this
document is analysis.

**This is a different bug from the one just fixed.** The `terrain_grid: 0` fix (adding
`terrain_grid: 256` to `rock-texture` in `assets/scenes/stylus-osm.yaml`) addressed a mesh-density
problem: sparse earcut boundary vertices undershooting sharp terrain between them. That fix is
correct and not touched here. This document is about a separate report: *"at some elevation
level, the landcover overlay gets stuck at that elevation, and wherever the actual elevation is
lower than that, the landcover floats like a flat layer above it, stuck at that elevation... rare,
could be a loading/network/race/caching thing."* Now that `terrain_grid` gives the mesh real
internal vertices, this shape (or its absence) is finally visible — before the fix, a mesh with
almost no internal vertices couldn't visually read as "a flat shelf" even if the underlying
elevation attachment were just as wrong.

## 1. Summary of the mechanism, with confidence level

**High confidence: the "shelf" is a raster attachment that never received its correct-resolution
elevation texture on its one and only fetch attempt, and nothing in the codebase ever retries or
re-fetches it afterward.** The recovery pass that exists (`TileManager`'s per-frame healing loop)
is *purely a passive cache lookup* — it can only notice and adopt a correct texture if something
**else**, unrelated to this raster attachment's own fetch, happens to independently populate the
shared texture cache at that exact tile coordinate. If nothing else ever does, the pin is
permanent for as long as the polygon tile stays continuously resident (visible, or held by a
child/parent proxy relationship) without ever being fully evicted and rebuilt from scratch.

Two independent, code-confirmed triggers feed the same broken end state:

1. **A subtask that gets canceled mid-flight** during pan/zoom churn (a `TileEntry` — and the
   `TileTask`s it owns — gets torn down before its raster subtask finishes, per
   `TileManager::TileEntry::clearTask()`).
2. **A subtask that fails for an entirely ordinary reason** — a real network error, timeout, or a
   cache-and-network double-miss — with **no churn, no cancellation, and no user action involved
   at all.**

Trigger 2 was not fully separated out in the earlier investigation
(`docs/terrain-drape-elevation-attachment-plan.md`'s Round 2/3, which focused specifically on
*cancellation*). Tracing the current code shows the two failure modes are actually
indistinguishable downstream — both end at the exact same `!isReady()` state and go through the
exact same (non-)recovery path — so trigger 2 deserves equal billing, and is arguably the more
probable contributor day-to-day: it needs no gesture-timing coincidence, just an ordinary blip.

## 2. Confirmed code trace

### 2.1 Both triggers converge on the same "subtask never got its data" state

**Cancellation** (`tangram-es/core/src/tile/tileManager.cpp:120-134`, `TileEntry::clearTask()`):
tearing down a `TileEntry` (e.g. a vector tile leaving view mid-fetch during a fast pan/zoom, or a
superseded entry created by rapid zoom-level churn) decrements `subtask->shareCount` and calls
`subtask->cancel()` once it hits zero. A subtask is only protected from this if some *other* owner
still references it (the `loadTiles()` dedup path, `tileManager.cpp:880-900`, which shares a
subtask across two main tasks that happen to want the same raster tile) — the ordinary case of a
single vector tile with its own private raster subtask has `shareCount == 1` and is fully
vulnerable.

**Genuine fetch failure**, confirmed at two independent layers with no interaction between them
needed:

- `NetworkDataSource::loadTileData`'s response callback
  (`tangram-es/core/src/data/networkDataSource.cpp:129`): `if (task->isCanceled()) { return; }` —
  note this check fires on `isCanceled()`, **not** on `response.error`. A **successful** HTTP
  response that arrives after the task was already canceled (for *either* reason above) is
  silently discarded here, in full, even though the bytes were already downloaded. There is no
  code path that inspects "did the data actually arrive, just too late" — canceled means dropped.
- `MBTilesDataSource::loadNextSource`'s `else` branch (`mbtilesDataSource.cpp:290-293`): when the
  disk-cache-then-network chain exhausts every source without data (real HTTP error, offline,
  timeout, whatever), it just logs `"missing tile"` and calls back with empty `rawTileData` — no
  retry, no backoff-then-retry, nothing queued for later.

Either way, `TileManager`'s own `m_dataCallback` (`tileManager.cpp:158-168`) sees a task with
`hasData() == false` and calls `task->cancel()` directly. **A genuine, one-off network failure and
a churn-cancellation are therefore literally the same downstream event** by the time
`RasterTileTask::complete(TileTask&)` (`rasterSource.cpp:655-663`) looks at it:

```cpp
void complete(TileTask& _mainTask) override {
    if (!isReady()) {  //isCanceled()?
        // check for alternative raster is now in TileManager
        auto source = rasterSource();
        _mainTask.tile()->rasters().emplace_back(tileId(), source->emptyTexture());
    } else {
        addRaster(*_mainTask.tile());
    }
}
```

A subtask that is `!isReady()` for *any* reason gets `emptyTexture()` attached, no distinction
made. Per `TileEntry::completeTileTask()` (`tileManager.cpp:99-118`), the *main* vector tile task
is still allowed to complete once every subtask is ready **or canceled** — so this doesn't stall
the tile, it just silently ships it with a wrong/empty elevation attachment.

### 2.2 The only recovery path is passive, cache-only, and narrowly bounded

`TileManager`'s per-visible-tile healing pass (`tileManager.cpp:605-661`, guarded by
`entry.numMissingRasters != 0`, which starts at `-1` so every fresh `TileEntry` gets at least one
pass) re-examines each attached raster every single frame the tile stays in `m_tiles` (i.e. stays
visible or holds proxy status). But every single thing it does is a **cache-only**
`RasterSource::getTexture()` lookup (`rasterSource.cpp:800-802` — a `weak_ptr::lock()` against
`m_textures`, erased the instant the last strong ref dies, `rasterSource.cpp:765-789`):

1. First it checks for the exact intended texture (`tileManager.cpp:639`). If found, it heals
   fully — but this can only succeed if the exact tile was cached by **someone else** in the
   meantime, since this fetch attempt already failed once and nothing re-issues it.
2. If not, it walks up **exactly three parent levels** looking for any cached ancestor
   (`tileManager.cpp:649-660`, `do { id = id.getParent(); ... } while (id.z > 13 || (id.z > 0 &&
   id.z + 2 >= tileId.z))`) — for a native-z14 vector tile this checks z13, z12, z11 and then
   **gives up entirely** if none of exactly those three specific tiles happen to be cached. (If
   none is found, the raster stays at the pure-empty texture — reading as elevation zero, a much
   more dramatic and probably more immediately-obvious symptom than "a plausible but wrong
   elevation." The "shelf sitting at a specific wrong-but-plausible height" report is more
   consistent with this walk actually landing on a real ancestor, i.e. one of z13/z12/z11 being
   broadly cached — very plausible on its own, since coarse tiles are shared by many surrounding
   fine tiles and so tend to have long-lived strong refs, unlike a single z14 leaf.)
3. Once *any* ancestor is pinned, `raster.tileID.z < intended.z` from then on
   (`tileManager.cpp:638`'s comment: *"A raster left at a coarser PARENT by an earlier pass...
   must keep being treated as missing so it HEALS once the intended texture lands in the source's
   shared cache"*) — this is explicitly a "wait and hope" design, not an active-repair one.

**No code anywhere issues a new fetch for the specific coordinate that failed.** A repo-wide
search for "retry" (`grep -rn retry tangram-es/core/src/data tangram-es/core/src/tile`) turns up
exactly one *unrelated* offline-download disk-write retry and the confirmed no-retry-just-backoff
comment on the unrelated neighbor-prefetch system (`tileManager.cpp:27-35`, and the fuller
 comment at `tileManager.cpp:767-772`) — nothing else.

### 2.3 Why the overzoom-composite mechanism can't bail this out either, once the pin lands far enough back

`upgradeAttachedRasters` (`tileManager.cpp:936-...`) re-attempts building a same-footprint overzoom
composite for a pinned/ancestor-attached raster every update, and its coverage-signature check
(`rasterSource.cpp:1074-1080`) *does* pick up newly-cached descendant data automatically — so in
principle this could patch over an ancestor pin without needing the exact original fetch to ever
succeed. But the composite's target zoom is hard-capped:

```cpp
// rasterSource.cpp:1005-1007
int zt = std::min<int>(m_zoomOptions.maxZoom, _primary.s);
zt = std::min<int>(zt, _primary.z + kOverzoomCompositeMaxLevels);  // kOverzoomCompositeMaxLevels == 2
```

`_primary.z` here is `raster.tileID.z` — **the pinned ancestor's own zoom**, not the vector tile's
native zoom. If the healing walk in §2.2 landed 2+ levels back (e.g. found z12 for an intended
z14, or the pure-empty case where nothing was even found), `zt` can reach at most
`primary.z + 2` = z14 in the best case, or the composite mechanism doesn't even apply at all in
the pure-empty case (there's no attached raster tileID to build a composite around in a
meaningful sense). Even in the best case, this composite mechanism can only ever recover
resolution equal to `pinned_zoom + 2` — **structurally incapable of ever reaching the correct
resolution if the pin ever lands more than 2 levels back**, no matter what gets cached later,
until the raster is healed back to something finer by §2.2's exact-match path. This is exactly the
mechanism flagged (for a different, ultimately-not-the-culprit bug) in
`docs/terrain-drape-cellcaps-investigation.md` §1.4 and the "Before, pinning a parent zeroed
`numMissingRasters`..." comment history at `tileManager.cpp:628-637` — confirmed still true of the
current code.

### 2.4 What would make the exact intended texture ever get cached "by someone else"

The healing pass's exact-match check (§2.2 step 1) can only succeed passively. The realistic
sources of "someone else" populating that exact cache slot are:

- **The elevation `RasterSource`'s own display traversal** (used independently for
  terrain-ground/hillshade) reaching the *exact same tile coordinate* at its *exact native zoom*
  for its own rendering purposes. This is plausible when the view's own terrain LOD naturally
  wants that same zoom there — but not guaranteed: the elevation tileset's own screen-area LOD
  (`tileManager.cpp`'s `getVisibleTiles` recursion) can legitimately settle on a different zoom
  than the vector tile's native zoom at that exact framing (this exact vector-vs-elevation LOD
  divergence, via a different formula, is the subject of
  `docs/terrain-drape-cellcaps-investigation.md`; the same disagreement that produces *that* bug
  also removes this healing pass's best chance of ever getting fed).
- **The neighbor-prefetch system** (`enqueueNeighborPrefetch`, `tileManager.cpp:773-...`,
  `docs/texture-shading-plan.md` Phase 1) — but this only prefetches same-zoom neighbors of tiles
  the elevation tileset is **already displaying**, so it inherits the same "only helps if
  elevation's own LOD happens to be at the right zoom nearby" limitation.
- **The vector tile itself getting rebuilt from scratch** — leaving view and re-entering creates a
  brand-new `TileEntry` with a brand-new subtask, a fresh, independent attempt. If the camera
  never revisits that exact framing again, this never happens.

**This converges into a coherent explanation for "rare" and "some tiles":** it requires (a) an
initial fetch failure/cancellation for that one tile's own raster subtask — individually
low-probability, but happens continuously across a session; (b) the healing walk landing more
than `kOverzoomCompositeMaxLevels` (2) zoom levels back, or missing entirely; and (c) the view
settling somewhere the elevation tileset's own independent traversal never happens to need that
exact coordinate at native zoom again. All three together are uncommon, which matches "rare," but
each is individually plausible on any given tile, which matches "some tiles, unpredictably."

## 3. Alternatives considered

- **`buildOverzoomElevationMosaic` picking stale data even when better is cached**
  (`rasterSource.cpp:1032-1139`): read the full stitch loop and the coverage-signature rebuild
  check. Found no bug — the signature explicitly hashes `(targetZoomPerCell, hasRealTextureBit)`
  per cell and forces a rebuild the moment more (or less) real data becomes available
  (`rasterSource.cpp:1074-1080`). One narrow theoretical gap: the signature hashes a *presence
  bit*, not a texture identity/generation — if a texture at the same `(x,y,z)` were evicted and a
  **different** texture object re-cached at the same coordinate within one update cycle (a tight
  eviction/refetch race), the signature could stay unchanged and skip a rebuild. This is far more
  contrived than §2's mechanism, wouldn't explain a *stable-at-rest, permanently wrong* shelf
  (only a transiently-stale one frame), and is not pursued further here.
- **MBTiles disk cache serving genuinely wrong data for a coordinate** (hash collision, TMS
  row-order bug, etc.): checked `MBTilesDataSource::getTileData`
  (`mbtilesDataSource.cpp:443-463`) — the TMS y-flip (`int y = (1 << z) - 1 - _tileId.y`) is
  applied consistently on both the read and write paths (`storeTileData`, same file). No
  coordinate-mapping bug found. This class of bug would also produce a patchy/scrambled-content
  signature, not a smooth "shelf," which doesn't match the report.
- **Stale-tile expiry logic re-requesting and silently keeping old data**
  (`mbtilesDataSource.cpp:175-194`, the `max_age`/`createdAt` staleness check): checked whether an
  mbtiles-cached elevation tile that has gone stale and fails its background refresh could end up
  worse off than before (empty/canceled) rather than merely stale. It doesn't: the stale bytes are
  captured up front into `stalecb` (`mbtilesDataSource.cpp:184-193`) and `loadNextSource`'s
  callback (`mbtilesDataSource.cpp:233-267`) calls that `stalecb` — not the plain `_cb` — whenever
  a stale copy was available, so a refresh failure still lands on the *stale but present* copy,
  not `emptyTexture()`. This path is therefore probably **not** a live contributor on its own; it's
  only a concern to the extent a stale copy read from a *genuinely wrong-for-its-coordinate* DEM
  tile (ruled out above) or an already-ancestor-pinned attachment (§2, the real mechanism) feeds
  into it.

## 4. What this document has NOT verified (honesty section)

- **Not run live.** Everything above is a static code trace against the current tree
  (`tangram-es/core/src/tile/tileManager.cpp`, `.../data/rasterSource.cpp`,
  `.../data/networkDataSource.cpp`, `.../data/mbtilesDataSource.cpp`), same policy as the prior
  three docs. No repro was attempted or observed.
- **Whether trigger 1 (cancellation) or trigger 2 (genuine fetch failure) is more common in
  practice** is not something static tracing can settle — both are real, both converge on the
  identical downstream state, and §5's diagnostic doesn't currently distinguish which one fired
  for a given occurrence (see §5's "what it does NOT tell you"). Telling them apart would need
  additional plumbing (e.g. a flag on `RasterTileTask` recording whether it was ever canceled vs.
  simply never had data) not added here to keep the diagnostic minimal.
- **Whether the healing walk's 3-level cap (§2.2 step 2) is actually what's hit in practice**, vs.
  the pure-empty (nothing found even in z13/z12/z11) case, is unconfirmed live. Both produce a
  visibly-wrong drape, but the pure-empty case should read as elevation-near-zero (a much starker,
  probably differently-described symptom) rather than "stuck at a plausible wrong elevation" — the
  report's wording leans toward the ancestor-pin case, but this is inference, not a direct
  observation.
- **Whether the elevation tileset's own LOD reaching the same exact tile independently (§2.4) is
  actually rare or actually common** at typical viewing framings is unconfirmed — it depends on
  the same vector-vs-elevation LOD-formula divergence traced (for a different symptom) in
  `docs/terrain-drape-cellcaps-investigation.md`, which that document itself flagged as not fully
  runtime-confirmed either.

## 5. TEMP DIAGNOSTIC — added now, logging only, marked clearly for later removal

**Added to `tangram-es/core/src/tile/tileManager.cpp` in this session** (both Debug and Release
builds compile clean with this change; `make -f tests.mk` still passes all 2037 assertions — this
change is purely additive logging with no behavior change). It is intentionally *left in place*
so the next live occurrence gets caught with real evidence, per the task's request for a
ready-to-deploy diagnostic rather than another after-the-fact investigation round.

**What it does:** two fields added to the private `TileManager::TileEntry` struct
(`tileManager.cpp:53-61`, right after the pre-existing `numMissingRasters` field):

```cpp
double pinStuckSince = 0.0;      // 0 == not currently stuck
double pinStuckLastLogged = 0.0; // last time the "stuck" LOGW fired for this entry
```

and a block added right after the existing per-frame healing loop
(`tileManager.cpp:663-709`, inside the same `if (entry.numMissingRasters != 0)` guard so it only
runs exactly when the pre-existing healing logic already runs — i.e. free of cost in the ordinary
"never had a missing raster" case):

- Re-derives, for each attached raster source, whether it is currently **fully missing**
  (`raster.texture == emptyTexture()`) or **pinned to a coarser ancestor**
  (`raster.tileID.z < intended.z`) — the exact same two states §2 traces.
- If either is true, starts (or continues) a wall-clock timer (`entry.pinStuckSince`) for this
  specific tile entry; if neither is true, resets the timer to 0 (transient gaps that heal within
  a frame or two never accumulate enough time to log).
- Once continuously stuck for **more than 3 seconds** (`kStuckThresholdSeconds`), emits, at most
  once per **15 seconds** per tile (`kStuckRelogSeconds`, to avoid log spam for a long-lived stuck
  tile) a warning visible at `LOG_LEVEL >= 2` — **this fires in Release builds too** (Release is
  built with `LOG_LEVEL=2`; `LOGW` requires only `>= 2`, confirmed in `log.h:64-67`), so Sebastian
  does not need a Debug build running to catch this live:

```
WARNING: TEMP DIAGNOSTIC (rock-texture-elevation-shelf-investigation.md): '<source>' tile <x/y/z/s>
elevation raster stuck missing/pinned (worst <N> zoom level(s) coarser than intended) for <T>s --
persistent, not transient
```

**What to watch for:** if the shelf bug is seen live, check the app's log output around that time
for this exact line. The tile id in the message identifies the `(x, y, z)` of the affected vector
polygon tile (its `s` reflects its current display/overzoom level) — cross-reference against
where the shelf appears on screen. A `worst` value of `0` means the raster never got *any*
ancestor and is likely reading near-zero elevation (see §4's open question); `worst >= 1` means an
ancestor N levels back is pinned, and per §2.3 this can only ever self-heal if `worst <=
kOverzoomCompositeMaxLevels` (2) *and* the exact right descendant data gets cached by something
else.

**What it does NOT tell you:** whether the original failure was a churn-cancellation or a genuine
network/cache failure (§2.1 — both converge before this diagnostic's vantage point), and it does
not itself fix anything — it is pure `LOGW` instrumentation with two `double` fields added to a
per-tile bookkeeping struct that already exists for the pre-existing (non-diagnostic) healing
logic.

**To remove later:** delete the two added fields (`tileManager.cpp:53-61`) and the added block
(`tileManager.cpp:663-709`, the `{ ... }` scope starting with the "TEMP DIAGNOSTIC" comment,
ending just before the pre-existing `}` that closes the `if (entry.numMissingRasters != 0)` block).
No other file touched.

## 6. Candidate fix directions (NOT implemented — for sign-off before any code change)

### (a) Make the healing pass actively re-fetch the intended tile, not just passively re-check

When the exact intended texture still isn't found after N consecutive frames (reusing this
session's new stuck-timer machinery as the trigger), issue a fresh, independent fetch for exactly
that `(x, y, z)` coordinate through the elevation source, rather than only ever checking whether
someone else happened to produce it.

- **Costs/risks:** needs a new, narrowly-scoped "fetch this exact raster tile for healing
  purposes only" path — distinct from the elevation tileset's own display-driven traversal and
  from `enqueueNeighborPrefetch`'s existing same-zoom-neighbor-of-visible-tile prefetch, since
  neither is keyed the right way (by "what a *draped vector tile* needs," rather than "what the
  elevation tileset itself wants to display"). Needs its own retry/backoff discipline so a
  genuinely-offline tile doesn't spin forever (mirroring the existing
  `kPrefetchFailureBackoffCount`/`kPrefetchFailureBackoffSeconds` pattern already used for
  neighbor prefetch, `tileManager.cpp:30-31`). This is the most complete fix — it directly
  addresses the "nothing ever retries" root cause identified in §2.2 — but is real new code with
  its own failure modes to get right (e.g. avoid duplicate-fetch races with the elevation
  tileset's own traversal wanting the same tile).

### (b) Raise or remove `kOverzoomCompositeMaxLevels`'s bite on this specific case

Since §2.3 shows the composite mechanism *would* self-heal from independently-cached descendant
data if only its `zt` ceiling weren't capped so close to the (possibly very coarse) pinned
ancestor, one option is to let the composite path search further past `primary.z +
kOverzoomCompositeMaxLevels` specifically when `primary` is itself a healing-pass ancestor pin
(not the ordinary overzoom case the constant was designed for).

- **Costs/risks:** conflates two different meanings of "primary" zoom (an ordinary overzoomed
  vector tile's own native zoom vs. a healing-pass ancestor substitute) that the current code
  treats identically; would need a way to distinguish them without adding new state that
  duplicates what this session's diagnostic fields already track. Still fundamentally a "hope
  something else populates the cache" fix, not a "make it definitely happen" fix — weaker than
  (a), though cheaper and lower-risk to implement.

### (c) Give the healing pass's exact-match check a strong-ref pin, independent of the transient attachment lifecycle

Keep a small, deliberately-retained strong reference to "this exact tile's native-zoom elevation
texture" scoped to the *lifetime of the vector tile that needs it* (not the elevation tileset's own
display-driven lifetime), so that even if the elevation tileset's own display moves away and would
otherwise let the weak-ref cache evict it, the specific coordinate this vector tile needs stays
resident once fetched even once.

- **Costs/risks:** doesn't address triggers 1/2 (an *initial* fetch failure still needs (a) or
  something like it) — this only helps the case where the fetch *did* eventually succeed once but
  then got evicted before the vector tile's own healing pass caught up to it. Real memory-lifetime
  tradeoff (more textures kept alive for longer specifically for this purpose) that needs sizing.
  Complementary to (a) rather than a substitute for it.

### (d) Symptom mitigation only: cap the visual severity of a large ancestor-pin delta

E.g. clamp how far `getTerrainMeshElevation()`'s read is allowed to diverge from a co-located
`terrain-ground` sample, similar in spirit to option (c) of
`docs/terrain-drape-resolution-plan.md`. Cheap, but leaves the underlying attachment wrong and
does nothing for the root cause — not recommended as more than a stopgap if (a)/(b)/(c) turn out
to be too invasive for the observed rarity of the bug.

## 7. Recommendation (Sebastian's call, not a decision)

Given the diagnostic in §5 is already live and cheap, the natural next step is simply to **wait
for the log line to fire during ordinary use** rather than speculatively implementing any of §6
now — the whole point of shipping the diagnostic first is to get a real, confirmed occurrence
(tile id, `worst` delta, duration) before committing to a fix whose design depends on exactly
which of §2.1's two triggers (or both) is actually the common case. If/when it fires, the tile id
in the log line, combined with `--view.lat`/`--view.lng`/`--view.zoom` matching where the shelf
was seen, gives a concrete repro to re-run under a Debug build with `LOGD` re-enabled for a closer
look at the "Healed"/"Found proxy" lines around that same tile id and timestamp.

Suggested command lines to have ready once a repro location is known (per CLAUDE.md conventions —
prefer a mountainous/varied-terrain area since that's where `bare_rock` polygons concentrate):

```
./build/Release/ascend --view.lat 49.38 --view.lng -123.20 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat 49.38 --view.lng -123.20 --view.zoom 13 --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
```
