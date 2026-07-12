#!/usr/bin/env bash
# Whole-process sampling fallback for the instrumentation profiler: catches hot spots that
# have no Profiler zones. Attaches `perf` to the running ascend process.
#
# Note: the Release build already compiles with -g (see make/unix.mk: CFLAGS += -MMD -g ...),
# so DWARF call graphs work without any build changes.
#
# usage:
#   record_perf.sh record [secs] [out.perf.data]   attach to running `ascend`, sample for N secs (default 10)
#   record_perf.sh report <out.perf.data>          print top functions (first 100 lines)

set -euo pipefail

cmd=${1:-}

case "$cmd" in
  record)
    secs=${2:-10}
    out=${3:-perf-$(date +%Y%m%d-%H%M%S).perf.data}
    pid=$(pgrep -n ascend) || { echo "error: no running 'ascend' process found" >&2; exit 1; }
    echo "sampling pid $pid for ${secs}s -> $out"
    perf record -F 999 --call-graph dwarf -p "$pid" -o "$out" -- sleep "$secs"
    echo "done; view with: $0 report $out"
    ;;
  report)
    data=${2:?usage: record_perf.sh report <out.perf.data>}
    perf report --stdio --percent-limit 0.5 -i "$data" | head -100
    ;;
  *)
    echo "usage: $0 record [secs] [out.perf.data] | report <out.perf.data>" >&2
    exit 1
    ;;
esac
