# Profiling System — Architecture & Implementation Spec

Goal: find where frame time goes while panning/zooming at mid-to-low zoom and in 3D, across
**main thread**, **tile worker threads**, and **GPU**, targeting a large (~20x) rendering-speed
improvement. Sebastian runs the app and captures traces; an analysis agent later reads a
*compact text summary* (never the raw trace) to rank optimization candidates.

Design: a custom always-compiled, runtime-toggled instrumentation profiler inside tangram-es
that writes **Chrome Trace Event JSON** (viewable in ui.perfetto.dev as a bonus), plus a
stdlib-only Python summarizer that reduces a multi-MB trace to a <150-line stats report.
No third-party deps (Tracy etc. rejected: binary format, needs GUI, poor fit for agent analysis).

Worktree: `/home/sebastian/projects/maps/.claude/worktrees/phase4-integration`
(branch `texture-shading-phase4-integration`; tangram-es submodule on same-named branch).

Hard constraints for all agents:
- Do NOT touch `assets/scenes/hillshade.yaml` or `docs/terrain-lighting-v2-plan.md` (unrelated in-progress work, uncommitted).
- Do NOT run `git commit` anywhere (outer repo or submodule). Leave changes in the working tree.
- Zone names passed to the profiler must be **string literals or interned pointers** — never a
  temporary `std::string::c_str()`.
- When capture is inactive, total overhead per zone must be one relaxed atomic load + branch.

## Phase A — Core profiler + capture control + summarizer

### A1. `tangram-es/core/src/debug/profiler.h` / `profiler.cpp`

```cpp
namespace Tangram {
class Profiler {
public:
    static bool isCapturing() { return s_active.load(std::memory_order_relaxed); }
    static void startCapture(const std::string& jsonPath);   // main thread
    static void stopCapture();                               // main thread; writes JSON
    static void frameMark();          // once per presented frame, main thread
    static void counter(const char* name, double value);     // sampled value, this frame
    static void counterAdd(const char* name, double value);  // accumulates within frame, reset at frameMark
    static void meta(const char* key, const std::string& value);  // capture-level metadata
    static void threadName(const char* name);                // call once per worker thread
    static const char* intern(const std::string& name);      // stable ptr for dynamic names (per-style, per-source)

    struct Zone {                     // RAII CPU zone
        Zone(const char* name);
        ~Zone();
        const char* m_name; int64_t m_t0; bool m_live;
    };
    struct GpuZone {                  // RAII GL timer-query zone, main GL context only
        GpuZone(const char* name);
        ~GpuZone();
        ...
    };
};
}
#define PROFILE_SCOPE(name)  Tangram::Profiler::Zone _profZone##__LINE__(name)
#define PROFILE_GPU(name)    Tangram::Profiler::GpuZone _profGpu##__LINE__(name)
```

Implementation requirements:
- Clock: `std::chrono::steady_clock`, ns since capture start.
- Per-thread event buffer (`thread_local`), registered once in a mutex-guarded global registry;
  zone entry/exit appends `{const char* name, int64 t0, int64 t1}` — no locks, no allocation
  per event (reserve in chunks). Hard cap ~4M events total → auto-stop capture with a log warning.
- `frameMark()`: emits a frame-boundary instant event and flushes accumulated `counterAdd`
  values as counter samples; also polls pending GPU queries (below).
- GPU zones: the context is **GLES 3.0** (`glfwWindowHint(GLFW_CLIENT_API, GLFW_OPENGL_ES_API)`
  in `app/src/glfwmain.cpp`). Use `GL_EXT_disjoint_timer_query` (`glQueryCounterEXT` /
  `GL_TIMESTAMP_EXT`) **if the extension is present**, else GpuZone is a silent no-op.
  Keep a query pool; resolve results at frameMark when `GL_QUERY_RESULT_AVAILABLE_EXT`;
  check `GL_GPU_DISJOINT_EXT` and drop affected samples. Emit resolved GPU spans on a synthetic
  "GPU" thread ID. Only the main GL context uses GpuZone (ignore the offscreen elevation worker).
- Output format (Chrome Trace Event JSON): `{"traceEvents":[...], "metadata":{...}}`
  - CPU span: `{"ph":"X","pid":0,"tid":<n>,"name":...,"ts":<µs>,"dur":<µs>}`
  - counter: `{"ph":"C","pid":0,"name":...,"ts":...,"args":{"v":<val>}}`
  - thread names: `{"ph":"M","name":"thread_name","tid":<n>,"args":{"name":...}}`
  - frame marks: `{"ph":"i","name":"frame","s":"g","ts":...}`
- `FrameInfo` bridge: in `debug/frameInfo.cpp`, `FrameInfo::begin/end` currently early-return
  unless the `tangram_infos` debug flag is set. Make them ALSO record profiler zones when
  `Profiler::isCapturing()` (via `intern(tag)`), so existing tags (Update, UI update/render,
  renderTerrainDepth) come for free. Keep on-screen behavior unchanged.

### A2. Capture control (app)

- In `app/src/glfwmain.cpp`, in the `app->win->addHandler` key handler: **Ctrl+P** toggles
  capture. Start: create `<MapsApp::baseDir>/profiles/` if needed, path
  `profiles/trace-YYYYMMDD-HHMMSS.json`; record metadata (git describe, zoom, pitch, camera
  position from `app->map`). Stop: write file, `PLATFORM_LOG` the path. Log both transitions.
- `Profiler::frameMark()` in the main loop right after `glfwSwapBuffers(glfwWin)`.
- Each frame while capturing, sample scenario counters (cheap, from MapsApp or Map): zoom,
  pitch (deg) — so the summarizer can segment by scenario.

### A3. `scripts/perf/summarize_trace.py` (outer repo)

Stdlib-only Python 3. `summarize_trace.py <trace.json> [--top N] [--slow-ms 16.6]`.
Streams/parses the trace and prints a report **< ~150 lines**:
1. Header: metadata, frame count, capture wall time, mean FPS.
2. Frame time distribution: mean / p50 / p90 / p99 / max (ms); % frames over `--slow-ms`.
3. Per-zone table sorted by total time (top N=25): name, thread, count, total ms, % of capture,
   mean, p95, max, avg count/frame. Separate section for GPU zones.
4. Per-thread busy % (sum of top-level spans / capture time).
5. Worst 10 frames: frame index, duration, top 5 zones inside that frame with ms.
6. Counters: name, min / mean / max (and last value).
Handle nested zones sensibly for the per-zone table (report inclusive time; add a
"self time" column = inclusive − child inclusive on same thread).

### A4. Sampling fallback: `scripts/perf/record_perf.sh`

Wrapper for whole-process sampling to catch anything not instrumented:
`perf record -F 999 --call-graph dwarf -p $(pgrep ascend) -o <out> -- sleep <secs>` plus a
`report` subcommand emitting `perf report --stdio --percent-limit 0.5 | head -100`.
Check the Release build compiles with `-g` (see `make/shared.mk`); if not, add `-g` to Release
C/CXX flags (negligible runtime cost, bigger binary is fine).

### A5. Acceptance (Phase A)

- `make -j$(nproc)` (Release) builds clean in the worktree; `make -f tests.mk` still passes.
- A tiny standalone check (can be a unit test in `tangram-es/tests/unit/` or a scripted run)
  exercises Zone/counter/frameMark/stop **without GL** and validates the JSON parses and the
  summarizer runs on it.
- Zero behavior change when not capturing.

## Phase B — Instrumentation pass

Add zones/counters (using Phase A API) at these points. Names in `CamelCase`, prefix dynamic
parts via `intern()`, e.g. `intern("style:" + style.getName())`.

Main thread (`tangram-es/core/src/`):
- `map.cpp` `Map::update`: sub-zones for scene time update, tileManager update, labels update,
  markers. `Map::render`: sub-zones upload/draw; wrap the whole render in PROFILE_GPU too.
- `style/style.cpp` per-style draw: CPU zone + `counterAdd("drawCalls",1)` per draw call site;
  a PROFILE_GPU per style pass is desirable if cheap to place.
- `tile/tileManager.cpp` `updateTileSets` (per source, interned), `upgradeAttachedRasters`.
- `labels/labelManager.cpp` update + collision detection.
- `marker/markerManager.cpp` update.
- `util/elevationManager.cpp` `renderTerrainDepth` (exists via FrameInfo — verify it shows up),
  any per-frame elevation queries.
- `gl/texture.cpp` upload path: zone + `counterAdd("texUploadKB", bytes/1024.0)`.
- `data/rasterSource.cpp`: mosaic build/stitch zones (`buildElevationMosaic`,
  `buildOverzoomElevationMosaic`, `flushDirtyMosaics`).
- App main thread (`app/src/mapsapp.cpp`): existing FrameInfo UI update/render tags suffice
  via the bridge; add a zone around `MapsApp::mapUpdate`.

Worker threads:
- `tile/tileWorker.cpp`: `threadName("TileWorker#<i>")` at thread start; zone per task
  (`intern("build:" + source name)`).
- `tile/tileBuilder.cpp`: sub-zones for data parse vs. per-style geometry build.
- The offscreen GL worker (`AsyncWorker` "Ascend offscreen GL worker"): threadName + CPU zones
  around enqueued elevation jobs if identifiable (`util/asyncWorker.cpp` or call sites).
- Network/decode threads if they do measurable CPU work (mbtiles fetch/decode in
  `data/mbtilesDataSource.cpp`): zone per fetch.

Per-frame counters (sampled in `Map::update` or frameMark call site):
- visibleTiles, proxyTiles, pendingTiles (tasks in flight), tileCache entries + KB,
  activeUrlRequests, labels active, markers, zoom, pitchDeg.

Acceptance (Phase B): builds clean (Release), tests pass, and a short headless or manual run
produces a trace whose summary shows: named worker threads, per-style zones, GPU zones (or a
logged note that the timer-query extension is unavailable), counters populated.

## Phase C — Capture & analysis loop (repeated)

1. Sebastian builds (`make -j`), runs, captures scenarios with Ctrl+P:
   (a) fast panning at z8–12 in 2D; (b) same in 3D tilted; (c) zoom in/out sweep; ideally one
   `record_perf.sh` sampling run of the same. Traces land in `~/.config/Ascend/profiles/`.
2. Run `scripts/perf/summarize_trace.py` on each trace.
3. A fresh analysis agent gets ONLY the summaries (+ perf report if taken), reads the relevant
   source, and returns a ranked list of optimization candidates with estimated impact and
   concrete change sketches. Raw traces stay on disk.
