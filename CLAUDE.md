# Visual verification policy

This project renders a map — whether a change "looks right" (label placement, font,
color, halo, icon sizing, curve quality, etc.) is Sebastian's call, not something to
self-grade.

- **Do not take headless screenshots** to self-verify visual/rendering changes, and
  **minimize unit testing** as a substitute for visual judgment. A green test suite does
  not mean the map looks right — it only means the code compiles and specific invariants
  hold. Prior sessions repeatedly shipped code that built clean, passed all unit tests,
  and still looked wrong (or crashed) until Sebastian looked at the live app.
- Instead, after making a change: **build both Debug and Release**, confirm they compile
  clean, and then hand back to Sebastian with the exact command to launch the app and
  look for himself. Do not claim a visual fix is "done" until he's confirmed it.
- Always paste a copy-pasteable command line, including:
  - The correct working directory / worktree (this repo currently has no extra
    worktrees in play — just `/home/sebastian/projects/maps` — but say so explicitly if
    that ever changes).
  - The binary path for both builds: `build/Debug/ascend` and `build/Release/ascend`.
  - A `--view.lat`/`--view.lng`/`--view.zoom` triplet for a relevant starting location
    when the change is location-specific (e.g. a peak-label fix should start over a
    mountainous area, not the default SF start position).

## Build commands

- Release: `make` (from repo root) → `build/Release/ascend`
- Debug: `make DEBUG=1` → `build/Debug/ascend`
- Unit tests (headless, fast, no display needed): `make -f tests.mk`

## Run command template

```
./build/Release/ascend --view.lat <LAT> --view.lng <LNG> --view.zoom <ZOOM> --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
./build/Debug/ascend   --view.lat <LAT> --view.lng <LNG> --view.zoom <ZOOM> --view.rotation 0 --view.tilt 0 --sources.last_source stylus-osm-terrain
```

Default start position if none given: Alamo Square, SF (`--view.lat 37.776444 --view.lng
-122.434668`), `zoom: 15`. For terrain/peak-label work, prefer a mountainous area — e.g.
`--view.lat 49.38 --view.lng -123.20 --view.zoom 13` (Lions/Cypress, BC, used in prior
label-placement sessions) or `--view.lat 45.9763 --view.lng 7.6586 --view.zoom 12`
(Zermatt/Matterhorn, used to test cluttered-terrain label collisions).

**Always include `--view.rotation 0 --view.tilt 0`** unless a tilted/rotated view is the
specific point of the test: the app persists camera rotation/tilt across sessions (same
mechanism as lat/lng/zoom), so a previous session's tilted state silently carries over and
both changes the framing and inflates render cost, confounding comparisons.

**Always include `--sources.last_source stylus-osm-terrain`** unless testing a specific
non-default map style is the point: `sources.last_source` also persists across sessions
in `~/.config/Ascend/config.yaml` and has been found stuck on `stylus-bike-hike` (which
force-enables `global.show_trails`, changing candidacy/priority behavior) — this exact
confound previously masked a real peak-visibility bug for an entire multi-round testing
effort before it was caught.
