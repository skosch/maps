# Label Prioritization & Placement — Redesign Spec (v2)

**Status: design document, not yet implemented.** Written 2026-07-16/17 after nine
incremental rounds of fixes to the existing system (`docs/label-placement-plan.md`,
1300+ lines, read that first for full historical context — this doc does not repeat
it). Intended for a fresh agent to implement from scratch, in a new worktree, with
Sebastian's sign-off at each phase. Do not start implementation without re-reading
the "Before you start" section below — the previous system's history is full of
plausible-sounding fixes that turned out to be solving the wrong problem.

## Why a redesign, not another round of fixes

Nine rounds of incremental fixes on the existing system each found a real, distinct
bug: a build-dependent sort tiebreak (raw pointer comparison), a fully dead code path
(an isolation term that silently never fired because its data source was never
resident), an O(labels × anchors × samples) redundant recomputation causing
25-98-second frame stalls, unbounded per-frame work, and a genuine use-after-free from
an overly-clever pointer relationship between sibling labels. None of these were
symptoms of one root cause — they were nine independent defects in a system that grew
organically, one user-reported symptom at a time, without a single coherent design.

The final, still-unresolved symptom (a specific named peak, the Matterhorn, provably
has correct high-priority data — real OSM `prominence=1038` — yet a fresh agent's
live testing could not reproduce it failing to show, while the user's real testing
consistently found it missing) is the proximate trigger for this redesign. See
"The Matterhorn contradiction" below for the leading hypothesis — it points at a
structural property of the current system (deliberately history-dependent collision
outcomes) that a redesign should treat as a first-class design constraint, not
something to special-case around.

## Before you start: what NOT to re-litigate

These are settled, working, and should be reused as-is unless you find a concrete
reason not to:
- Real OSM `prominence`/`ele`/`wikipedia` tags are the best available salience signal
  and should always win when present. The texture-shading/isolation machinery exists
  only as a fallback for the common case where they're absent.
- The CPU texture-shading sampler (`textureShading.cpp`, `sampleTextureShadingAtMosaic`)
  is a literal port of the live GPU shader's formula and must stay that way — don't
  invent a new heuristic. Its single-slot weak_ptr-keyed pyramid cache (round 8) is a
  real, measured 5-8x win and should be preserved or generalized, not removed.
- Threading: `RasterSource`/`ElevationManager` raster access is main-thread only.
  `TextStyleBuilder`/`PointStyleBuilder::addFeature` run on `TileWorker` threads and
  cannot touch raster data — this was investigated exhaustively (see
  `docs/label-placement-plan.md`'s Phase 2 threading section) and is not worth
  re-investigating.
- The per-frame work budget pattern (round 9, `kMaxAnchorRefinementsPerFrame`) is the
  right shape of fix for "too much one-time work due in a single frame" — generalize
  it, don't remove it.
- Don't take headless screenshots to self-verify visual correctness (see this repo's
  `CLAUDE.md`). Build both Debug and Release, verify they compile and pass
  `make -f tests.mk`, and hand back exact run commands (including
  `--view.rotation 0 --view.tilt 0`, and the actual `--sources.last_source` to use —
  see "The map-source trap" below) for Sebastian to check visually.

## The Matterhorn contradiction (unresolved — resolve this FIRST)

Confirmed facts, not speculation:
- Matterhorn (`osm_id=26863664`, `natural=peak`) has real `prominence=1038`,
  `ele=4478` in the vector tile data (`assets/cache/stylus-osm.mbtiles`, z12/x2135/y2638
  — decoded directly with a hand-rolled MVT parser, not guessed).
- Its priority is therefore computed entirely by the tile-build-time JS function using
  the real tag (`stylus-osm.yaml`'s peak `priority:` function), landing at
  `≈14.16` — deep inside the best part of the named-peak band `[14.0, 14.4]`, and
  numerically far ahead of every non-peak competing category (roads ≥23, `route_minor`
  piste/hike text =65 or unset/`FLT_MAX`). The isolation/texture-shading refinement
  machinery (rounds 7-9 of the old plan) never runs for it at all, because
  `suppressTextureShadingIfProminent()` disables that path whenever a real `prominence`
  tag is present.
- Extensive live instrumentation (tagging the exact label object, tracing it through
  candidacy → priority computation → collision resolution → the text-mesh renderer)
  consistently showed it winning every stage — never occluded, reaching `visible`
  state with `alpha=1.0` and real glyph geometry — across multiple test conditions
  (default camera, default map source, both explicitly reset to rule out two real,
  separately-confirmed confounds — see below). This was Release-build, headless,
  software-rendered (Mesa llvmpipe under Xvfb).
- The user's real testing, on real hardware, consistently does not show the label.

**Two real confounds were found and ruled out** (both worth guarding against in the
redesign's own test/verification tooling, since they cost significant time to find):
1. **Camera rotation/tilt persists across app sessions** (`view.rotation`/`view.tilt`
   in `~/.config/Ascend/config.yaml`, same mechanism as lat/lng/zoom). A stale tilted
   camera from earlier testing silently changed both the framing and the render cost
   of every subsequent test until explicitly reset via `--view.rotation 0 --view.tilt 0`.
2. **The map source also persists** (`sources.last_source` in the same config file) and
   was found stuck on `stylus-bike-hike` rather than the default `stylus-osm-terrain` —
   this is the *exact* bug class from an earlier round of the old plan ("every
   verification screenshot used stylus-bike-hike, masking a real bug" —
   `docs/label-placement-plan.md`'s round-3 addendum). Override explicitly with
   `--sources.last_source stylus-osm-terrain` for reproducible testing. Both sources
   were tested for Matterhorn specifically; both showed the label succeeding.

**Leading hypothesis, not yet confirmed**: `LabelManager::priorityComparator()`
contains this comment, verbatim, at its own tiebreak stage 6:
```cpp
// Note: This causes non-deterministic placement, i.e. depending on
// navigation history.
if (l1->occludedLastFrame() != l2->occludedLastFrame()) {
    return l2->occludedLastFrame();  // non-occluded over occluded
}
```
This is a deliberate anti-flicker hysteresis: once a label wins a frame, it's
preferentially kept winning in later frames even against a competitor with
technically-better priority, specifically to avoid labels flickering in and out as
tiny priority differences flip back and forth. **This means the system's collision
outcome for near-tied competitors is not a pure function of label data — it depends on
which one happened to render first**, which in turn depends on tile-arrival order,
network timing, and frame-by-frame history that can genuinely differ between two
runs of the identical code on different machines (or even the same machine on
different days, given network-dependent tile loading). If Matterhorn is in close
screen-space proximity to another peak or a large cluster of competing labels (it's
in one of the most cluttered viewing areas ever used to test this system — see the
`Klein Matterhorn`, `Matterhorngletscher`, and multiple `Matterhorn`-named viewpoint/
museum POIs found in the same and adjacent tiles during this investigation), a small
timing difference in which label's collision check ran first during initial tile
load could plausibly lock in a persistently-different winner between two sessions —
this would exactly explain "same code, consistently different outcome, on different
machines."

**This must be the first thing the implementing agent confirms or refutes**, ideally
by:
1. Getting Sebastian to reproduce with a precise repro (exact command, exact pan/zoom
   history if any, ideally a screenshot) rather than re-guessing confounds blind.
2. Adding instrumentation (temporary, reverted before finishing — see the existing
   session's pattern of tagging one label with a `bool m_debugWatch` field and tracing
   it through `handleOcclusions()`) that specifically logs the `occludedLastFrame`
   hysteresis branch: does Matterhorn's label (or its `relative()` icon) ever take that
   branch, and against what competitor?
3. If confirmed, design a principled replacement for the hysteresis — see
   "Deterministic-but-not-jittery collision resolution" below.

## Design goals

1. **Deterministic**: for a fixed camera position, zoom, map source, and fully-loaded
   tile set, the SAME set of labels must be chosen and placed the SAME way, every time,
   regardless of tile arrival order, thread scheduling, or which frame settling
   happened to be observed. (The old system explicitly does not have this property —
   see above.) "Fully loaded" is doing real work in that sentence; transient states
   during loading are allowed to differ, but the system must converge to one answer.
2. **Fast, with a hard per-frame ceiling**: no single frame should ever do more than a
   bounded amount of expensive per-label work, regardless of how many candidate labels
   are in view. (Round 9's `kMaxAnchorRefinementsPerFrame` budget is the right shape —
   generalize it, and make the ceiling a measured time budget rather than a fixed
   label count, since "150 labels" costs a wildly different amount of wall-clock time
   in a Debug vs Release build, as this session's profiling directly measured — 5.7s
   vs 77s for comparable label counts.)
3. **Correct given correct inputs**: a peak with strong real prominence data must
   reliably outrank weaker candidates. This sounds obvious but is exactly the property
   currently unconfirmed for Matterhorn.
4. **Legible priority model**: one person should be able to predict, by reading the
   priority of two labels, which one wins, without needing to trace a 12-stage
   tiebreak chain through implicit float-rounding behavior. (See "The `nearbyint`
   tier-bucketing risk" below — a real, latent edge case in the current comparator
   that a legible model would not have.)
5. **No silently-dead code paths**: the isolation-term bug (rounds 7-8 of the old
   plan) shipped a whole feature that never activated because its data assumption was
   wrong, and nothing caught this until a human noticed the user-visible symptom
   didn't change. The redesign should make "is this term ever actually contributing"
   observable (e.g., via existing `gui_variables` debug toggles, or a debug overlay
   counter) rather than requiring live instrumentation to discover.

## Non-goals (unchanged from the old plan, still correct)

- Mountain ridge-line labeling (`natural=ridge`/`arete`) — sparse OSM coverage, its
  own multi-week project.
- True saddle-based topographic prominence (global watershed/flood-fill search) — use
  real OSM `prominence` when present, a local proxy otherwise. Not a geodesy-grade
  prominence database.
- Do not merge to `master` or push without explicit human approval.

## Known structural risks to fix (found during this session's research pass)

### The `nearbyint` tier-bucketing risk
`priorityComparator`'s first real comparison after the proxy check is:
```cpp
float aprio = std::nearbyint(_a.priority);
float bprio = std::nearbyint(_b.priority);
if (aprio != bprio) { return aprio < bprio; }
```
Priority values are deliberately designed as `tier + fraction` (e.g. peak =
`[14.0, 14.9]`), and `nearbyint` rounds to the nearest integer using round-half-to-even.
An **unnamed** peak's band is `[peak+0.5, peak+0.9]` = `[14.5, 14.9]`.
`nearbyint(14.5) = 14` (ties-to-even, 14 is even) but `nearbyint(14.6) = 15` — meaning
unnamed peaks with a fractional priority just above the tie point silently round into
the SAME coarse tier as `saddle` (15), a category one full integer away in the
intended design. This is a real, confirmed-by-reading-the-code latent bug, not
speculation — whether it's ever been the actual cause of a reported symptom is
unconfirmed, but it should not exist in a "legible priority model" redesign. **Fix
approach**: use `floor`, not `nearbyint`, for tier extraction (matching every other
tier-floor computation in this codebase, e.g. `Label::refinePriority()`'s own
`std::floor(m_options.priority * 2.f) / 2.f` for half-tier bands) — or better, store
tier and within-tier score as two separate fields (see "Proposed architecture" below)
so there is no float-rounding step at all.

### Shared `repeatGroup` across every same-draw-rule peak
Every peak name label defaults to `repeatGroup = _rule.getParamSetHash()` — identical
for every peak built from the one `peak:` draw rule, since the hash is over style
parameters, not per-feature content. Combined with a 256px (at `pixelScale()=1`)
default `repeatDistance`, this means **any two peaks within 256 screen pixels of each
other mutually exclude regardless of their relative priority difference** — the first
one processed (by priority order, so normally the more prominent one, but see the
history-dependence risk above) wins the repeat slot and the other is unconditionally
occluded before real per-anchor collision testing ever runs. In a cluttered alpine
view (exactly the Matterhorn test case — multiple named 3800-4600m peaks within a few
km of each other) this is a very plausible mechanism for a genuinely more-prominent
peak losing outright to a merely-processed-first neighbor, independent of the
anti-flicker hysteresis question above. **Fix approach**: either scope repeat-groups
by actual geographic distinctness (e.g., only same-named-feature deduplication, which
was the original documented purpose — "deduplicating genuinely-coincident sprites,
the same summit double-tagged in OSM" — not general same-style mutual exclusion), or
make the repeat-group loser fall through to real per-anchor collision testing instead
of an unconditional occlude, so a losing peak still gets a chance to place somewhere
non-overlapping rather than disappearing outright.

### Two-stage priority computation with ad hoc clamping
Priority is computed once at tile-build time (JS, provisional or final depending on
tag availability) and then possibly overwritten at main-thread refinement time (C++,
`bandFloor - min(compressTextureShading(shade) + kIsolationWeight*isolation, 0.39f)`).
The `0.39f` cap exists specifically to prevent the combined compression from
spilling past the intended 0.4-wide band into the next integer tier — a hand-tuned
safety margin, not a structural guarantee. Every time a new term is added to this
formula (as happened three times across rounds 2, 7, and 9 of the old plan), someone
has to remember to re-verify the combined maximum still fits under the cap. **Fix
approach**: separate "tier" (an integer, or enum, chosen once and never touched by
refinement) from "score" (an unbounded or clamped-to-`[0,1]` continuous value used
only to order within a tier) as genuinely separate fields, so adding a new scoring
term can never cross a tier boundary by construction, not by convention.

## Proposed architecture

### Data model
Replace the single `float priority` with two fields:
```cpp
struct LabelPriority {
    int32_t tier;      // category, e.g. peak=14, saddle=15 -- set once, tile-build time, never refined
    float score;        // [0, 1], continuous salience WITHIN the tier, 0=best, higher=worse
};
```
Sort key becomes lexicographic `(tier, score)` — no rounding, no ambiguity, no
tier-boundary edge cases possible by construction. `score` for peaks:
- Real `prominence` tag present: `1 - sqrt(min(1, prominence / 3000))` (unchanged
  formula, still principled).
- No real tag: combine local texture-shading (`compressTextureShading`-equivalent) and
  regional isolation (`isolationScore`-equivalent) as an explicit weighted sum, but
  since `score` is not tier-adjacent anymore, no clamping-below-a-magic-cap is needed
  — just clamp to `[0,1]` directly, which is a real invariant of `score`'s type, not a
  hand-tuned safety margin.
- Named vs. unnamed: keep as **separate tiers** (`tier=peak_named`, `tier=peak_unnamed`
  as two adjacent integers, e.g. 14 and 15, or use a sub-tier fixed-point scheme if
  tier values need to stay meaningful across categories) rather than sub-bands of one
  float — this is the direct fix for the `nearbyint` risk, since there is no longer a
  float boundary to round across.

### Priority computation stages (unchanged in spirit, cleaner in mechanism)
1. **Tile-build time (JS or C++, TileWorker thread)**: assign `tier` and a provisional
   `score` from whatever's synchronously available (real tags, or a fixed
   worst-in-tier score if none). This stage's output must be a legitimate, final
   answer on its own — refinement is optional enhancement, never a requirement for a
   label to be placeable at all (this property already holds in the old system and
   should be kept).
2. **Main-thread refinement (once per label, retry-until-available, budgeted per
   frame)**: for peaks without real tags, replace `score` using texture-shading +
   isolation. Same threading constraints as today (main-thread raster access only).
   Never touches `tier`.

### Candidacy filtering
Keep the current `natural=peak AND ele>=1 AND (wikipedia OR prominence OR
ele>=candidacy_floor OR zoom>=12)` structure — this was already fixed correctly
(round 3 addendum's `show_trails` regression) and no issue was found with it in this
session's research. Do re-verify the `ele: {min: 1}` filter's exclusion of named
peaks with no elevation data at all — flagged as a real, still-open gap in the old
plan (round 3 addendum) and never fixed.

### Deterministic-but-not-jittery collision resolution
This is the crux of the redesign. The goal is to keep near-tied competitors from
flickering frame-to-frame WITHOUT making the final winner depend on incidental
load-order history. Two candidate approaches, in order of preference — pick one and
document the choice, don't leave both half-implemented:

**Option A (preferred): settle once, then freeze, using a stable key, not frame
history.** Instead of `occludedLastFrame()` as a tiebreak (which encodes "who won
last frame," a moving target during initial load), use a deterministic tiebreak that
never changes once computed — e.g., the existing `Label::id()` creation-order serial
(already added in round 6, already proven build-independent and deterministic within
a session) or, better, a genuinely content-derived stable key (e.g. a hash of the
feature's OSM id, if plumbed through — more robust than creation order, which still
depends on tile-build parallelism across different tiles). Combined with hysteresis
that only kicks in for labels that are ALREADY in a stable `visible` state for N
consecutive frames (not "won the most recent single frame," which can be a transient
loading artifact) — so flicker-avoidance only protects genuinely-settled state, not
whatever happened to render first during load.

**Option B: two-pass resolution.** First pass: resolve collisions using ONLY
`(tier, score, stable_id)` — no history dependence at all, giving a fully
deterministic result. Second pass, only for animation/transition purposes (not for
the final "did this label win" decision): apply fade timing so the transition from
frame to frame looks smooth, without ever changing WHICH label the first pass
decided should win. This cleanly separates "what's the correct answer" from "how do
we animate toward it," which the current system conflates.

Whichever is chosen, add a unit test asserting: given a fixed set of labels with
fixed priorities and a fixed processing order, `handleOcclusions()` produces the
same winner regardless of how many times it's re-run or in what frame-history state
it starts from. This is the single most important new test this redesign should add
— it's the property the current system provably lacks.

### Anchor selection
Keep the two-stage design (tile-build-time `salienceOrderedAnchors()` for vector
proximity, main-thread `refineAnchor()` for ridge/canyon avoidance) — it's sound in
concept. Two changes:
1. **Reduce the sample cost, or make it genuinely O(1) amortized.** 5 samples ×
   up to 9 anchors = up to 45 `sampleTextureShading()` calls per label is the root
   cause the round-9 budget had to work around. Consider precomputing a coarse
   "ridge cost field" once per mosaic (e.g. a downsampled grid of `ridgeCost` values,
   computed once when the mosaic's pyramid is first built — reusing round 8's cache
   point exactly) and having each anchor candidate look up its footprint's cost via a
   handful of grid lookups instead of full `sampleTextureShadingAtMosaic` calls. This
   was explicitly researched and left unimplemented in the old plan (round 4's
   citation of Kittivorawong et al.'s occupancy-bitmap approach, and Luboschik et
   al.'s particle-contour sampling) — revisit that research before reinventing it.
2. **Keep the per-frame budget, but size it by measured time, not label count.**
   Track wall-clock time spent in `refineAnchor()` calls this frame; stop issuing new
   ones once a budget (e.g. 4ms, tunable) is exceeded, not once a fixed count is hit.
   This makes the same code behave sanely in both Debug and Release without needing
   two different tuned constants.

### Caching discipline (generalize round 8's fix)
State explicitly, as an architectural rule for this module: any computation whose
cost depends only on the mosaic/tile data (not on the specific sample point or
label) must be cached keyed by a `weak_ptr` to the underlying resident texture, not
recomputed per call. Round 8 fixed exactly one instance of this (the texture-shading
pyramid); the redesign should audit for others before shipping (e.g., does
`AnchorOccupancyGrid` sampling have a similar per-call redundancy? It's already
built once per tile, so likely fine — verify, don't assume.)

## Adjustable parameters (complete list, carried over + redesign additions)

| Parameter | Old value | Role | Redesign note |
|---|---|---|---|
| `kIsolationWeight` | 0.15 | Isolation term's weight within `score` | Keep as a tunable; no longer needs the 0.39 tier-boundary cap since `score` is tier-independent |
| `kIsolationMarginMeters` | 300m | Isolation score ramp width | Keep, tune by eye |
| `kMaxAnchorRefinementsPerFrame` | 150 (count) | Per-frame refinement budget | **Change to a time budget** (e.g. `kAnchorRefinementFrameBudgetMs = 4.0`), see above |
| `peak_candidacy_ele_floor` | 500m | Candidate-pool-size bound below z12 | Keep, already correctly documented as NOT a salience signal |
| `anchorGapScale` (peak name, elevation) | 0.25 | Icon↔label distance | Keep, already unified across name/elevation per round 5/6 fixes |
| `kOccupancyWeight` | 1.0 | Vector-clutter vs. ridge weight in anchor cost | Keep, tune by eye |
| `kNumSamples` (anchor footprint) | 5 | Samples per anchor candidate | Reconsider if switching to a precomputed cost field (may become moot) |
| `kMaxLevels` (texture-shading pyramid) | 4 | CPU sampler octave depth | Keep — tied to 3×3 mosaic validity, not arbitrary |
| `kAlpha`, `kContrast` | 0.6, 1.0 | Texture-shading formula constants | Keep — must match live shader defaults, verify they haven't drifted again (this exact drift happened once already, see `docs/texture-shading-plan.md`) |
| `repeat_distance` (peak name) | 256px default | Repeat-group mutual exclusion radius | **Revisit per "shared repeatGroup" risk above** — likely needs to shrink or be scoped differently |
| New: hysteresis settle threshold | n/a | How many consecutive frames a label must hold `visible` before flicker-protection applies | New parameter for Option A above, needs a starting value + visual tuning |

## Testing & verification strategy

- **Unit tests** (pure logic, no GL/ElevationManager needed, matching the existing
  `sortAnchorIndicesByCost`/`isolationScore` pattern): tier/score computation from
  synthetic tags; the new determinism property test described above; the anchor
  cost/ordering functions.
- **`make -f tests.mk`** must stay green throughout — it's fast and headless, keep
  using it liberally during implementation.
- **No self-verification via headless screenshots.** Build both `make DEBUG=0` and
  `make DEBUG=1`, confirm clean compiles, and hand back exact run commands (see
  template below) for Sebastian's visual sign-off at each phase.
- **Explicitly test the Matterhorn case** as a first-class regression check once the
  contradiction above is root-caused: whatever mechanism is found, add a scenario
  (either a unit test with synthetic data reproducing the exact tier/score/repeat-group
  values, or a documented manual-check step) that would have caught it.

Run command template (do not skip the rotation/tilt/source resets — see the two
confounds documented above):
```
./build/Release/ascend --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5 \
    --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat 45.9763 --view.lng 7.6586 --view.zoom 12.5 \
    --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
```
Also worth checking against a second real-world case already used in prior sessions
(East/West Lions, Vancouver North Shore — similar height to neighbors, tests whether
genuinely-prominent-but-not-tallest peaks still win):
```
./build/Release/ascend --view.lat 49.38 --view.lng -123.20 --view.zoom 13 \
    --view.rotation 0 --view.tilt 0
```

## Suggested implementation order

1. Root-cause the Matterhorn contradiction (via the hysteresis hypothesis or
   otherwise) BEFORE writing any new architecture — you need to know which structural
   risk above is actually load-bearing, not guess.
2. Implement the `tier`/`score` split (data model change) — this is a mechanical
   refactor of existing formulas into the new shape, low risk, immediately fixes the
   `nearbyint` risk by construction.
3. Implement whichever deterministic-collision option (A or B) was chosen, with the
   new determinism unit test passing.
4. Address the shared-`repeatGroup` risk.
5. Only then, if still needed, revisit the anchor-sampling cost (precomputed field vs.
   time-budgeted sampling) — this is a performance optimization, not correctness, and
   round 9's existing budget mechanism is a reasonable stopgap if time runs short.
6. Full integration pass: both builds clean, `make -f tests.mk` green, hand back to
   Sebastian with exact run commands for every test location used above.

Do not merge to `master` or push without explicit approval, per this project's
standing rule.
