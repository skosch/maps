#!/usr/bin/env python3
"""Summarize a Chrome Trace Event JSON produced by the Tangram Profiler (Ctrl+P in the app).

Reduces a multi-MB trace to a short stats report (~<150 lines) suitable for analysis by a
human or an agent without loading the raw trace. Stdlib only.

Usage: summarize_trace.py <trace.json> [--top N] [--slow-ms 16.6]
"""

import argparse
import json
import sys
from bisect import bisect_right
from collections import defaultdict


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def fmt_ms(us):
    return us / 1000.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace")
    ap.add_argument("--top", type=int, default=25, help="rows in the per-zone table (default 25)")
    ap.add_argument("--slow-ms", type=float, default=16.6, help="slow-frame threshold in ms")
    args = ap.parse_args()

    with open(args.trace, "r") as f:
        data = json.load(f)

    events = data.get("traceEvents", [])
    metadata = data.get("metadata", {})

    thread_names = {}                 # tid -> name
    spans_by_tid = defaultdict(list)  # tid -> [(ts, dur, name)]
    counters = defaultdict(list)      # name -> [(ts, value)]
    frame_ts = []

    for ev in events:
        ph = ev.get("ph")
        if ph == "X":
            spans_by_tid[ev.get("tid", 0)].append((ev["ts"], ev.get("dur", 0.0), ev.get("name", "?")))
        elif ph == "C":
            counters[ev.get("name", "?")].append((ev["ts"], ev.get("args", {}).get("v", 0.0)))
        elif ph == "i" and ev.get("name") == "frame":
            frame_ts.append(ev["ts"])
        elif ph == "M" and ev.get("name") == "thread_name":
            thread_names[ev.get("tid", 0)] = ev.get("args", {}).get("name", "?")

    frame_ts.sort()
    t_end = 0.0
    for tid, spans in spans_by_tid.items():
        spans.sort(key=lambda s: (s[0], -s[1]))
        for ts, dur, _ in spans:
            t_end = max(t_end, ts + dur)
    if frame_ts:
        t_end = max(t_end, frame_ts[-1])
    capture_us = max(t_end, 1e-9)

    # Per-zone stats with self time (inclusive - direct children inclusive, same thread) and
    # per-thread busy time (top-level spans only), via a nesting stack per thread.
    zone = defaultdict(lambda: {"count": 0, "total": 0.0, "self": 0.0, "durs": [], "max": 0.0})
    busy_by_tid = defaultdict(float)
    for tid, spans in spans_by_tid.items():
        tname = thread_names.get(tid, "tid%d" % tid)
        stack = []  # [end_ts, child_total, name, dur]

        def finalize(fin, tname=tname):
            zone[(fin[2], tname)]["self"] += max(fin[3] - fin[1], 0.0)

        for ts, dur, name in spans:
            while stack and ts >= stack[-1][0] - 1e-9:
                finalize(stack.pop())
            if stack:
                stack[-1][1] += dur
            else:
                busy_by_tid[tid] += dur
            stack.append([ts + dur, 0.0, name, dur])
            z = zone[(name, tname)]
            z["count"] += 1
            z["total"] += dur
            z["durs"].append(dur)
            z["max"] = max(z["max"], dur)
        while stack:
            finalize(stack.pop())

    frames = [b - a for a, b in zip(frame_ts, frame_ts[1:])]
    nframes = len(frame_ts)

    print("=" * 78)
    print("Trace: %s" % args.trace)
    for k, v in metadata.items():
        print("  %s: %s" % (k, v))
    wall_ms = fmt_ms(capture_us)
    fps = (len(frames) / (wall_ms / 1000.0)) if (frames and wall_ms > 0) else 0.0
    print("  frames: %d   capture wall time: %.1f ms   mean FPS: %.1f" % (nframes, wall_ms, fps))

    if frames:
        fs = sorted(frames)
        slow = sum(1 for d in frames if fmt_ms(d) > args.slow_ms)
        print("\n-- Frame time (ms) over %d frame intervals --" % len(frames))
        print("  mean %.2f   p50 %.2f   p90 %.2f   p99 %.2f   max %.2f" % (
            fmt_ms(sum(fs) / len(fs)), fmt_ms(percentile(fs, 50)), fmt_ms(percentile(fs, 90)),
            fmt_ms(percentile(fs, 99)), fmt_ms(fs[-1])))
        print("  frames over %.1f ms: %d (%.1f%%)" % (args.slow_ms, slow, 100.0 * slow / len(frames)))

    def zone_table(title, items):
        print("\n-- %s (top %d by total time) --" % (title, args.top))
        hdr = "%-30s %-12s %7s %9s %6s %8s %8s %8s %8s %7s" % (
            "zone", "thread", "count", "total_ms", "%cap", "self_ms", "mean_ms", "p95_ms", "max_ms", "n/frame")
        print(hdr)
        for (name, tname), z in items[:args.top]:
            durs = sorted(z["durs"])
            print("%-30s %-12s %7d %9.1f %5.1f%% %8.1f %8.3f %8.3f %8.3f %7.1f" % (
                name[:30], tname[:12], z["count"], fmt_ms(z["total"]),
                100.0 * z["total"] / capture_us, fmt_ms(z["self"]),
                fmt_ms(z["total"] / z["count"]), fmt_ms(percentile(durs, 95)),
                fmt_ms(z["max"]), (z["count"] / len(frames)) if frames else 0.0))

    cpu_zones = sorted(((k, z) for k, z in zone.items() if k[1] != "GPU"),
                       key=lambda kz: -kz[1]["total"])
    gpu_zones = sorted(((k, z) for k, z in zone.items() if k[1] == "GPU"),
                       key=lambda kz: -kz[1]["total"])
    if cpu_zones:
        zone_table("CPU zones", cpu_zones)
    if gpu_zones:
        zone_table("GPU zones", gpu_zones)
    else:
        print("\n-- GPU zones: none (no GPU instrumentation or timer queries unavailable) --")

    if busy_by_tid:
        print("\n-- Per-thread busy % (top-level span time / capture time) --")
        for tid in sorted(busy_by_tid, key=lambda t: -busy_by_tid[t]):
            print("  %-20s %6.1f%%  (%.1f ms)" % (thread_names.get(tid, "tid%d" % tid),
                  100.0 * busy_by_tid[tid] / capture_us, fmt_ms(busy_by_tid[tid])))

    if frames:
        # worst frames: spans are attributed to the frame interval containing their start
        worst = sorted(range(len(frames)), key=lambda i: -frames[i])[:10]
        worst_set = set(worst)
        per_frame = {i: defaultdict(float) for i in worst_set}
        for tid, spans in spans_by_tid.items():
            for ts, dur, name in spans:
                i = bisect_right(frame_ts, ts) - 1
                if i in worst_set:
                    per_frame[i][name] += dur
        print("\n-- Worst 10 frames (top 5 zones by inclusive ms within frame) --")
        for i in worst:
            tops = sorted(per_frame[i].items(), key=lambda kv: -kv[1])[:5]
            desc = "  ".join("%s=%.1f" % (n[:22], fmt_ms(d)) for n, d in tops)
            print("  frame %4d  %7.1f ms | %s" % (i, fmt_ms(frames[i]), desc))

    if counters:
        print("\n-- Counters --")
        print("  %-24s %10s %10s %10s %10s %7s" % ("name", "min", "mean", "max", "last", "samples"))
        for name in sorted(counters):
            vals = [v for _, v in counters[name]]
            print("  %-24s %10.2f %10.2f %10.2f %10.2f %7d" % (
                name[:24], min(vals), sum(vals) / len(vals), max(vals), vals[-1], len(vals)))

    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
