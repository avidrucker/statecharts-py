#!/usr/bin/env python3
"""Zero-dependency benchmark harness for the statecharts engine.

Establishes a throughput baseline (events/sec) across three chart shapes that
stress different parts of the algorithm:

* **wide**  — one ``parallel`` with N ping/pong regions; every event fires one
  transition *per region*, stressing the optimal-enabled-set computation across
  parallel regions.
* **deep**  — N nested compound states with two toggling leaves at the bottom.
  The toggle's LCCA is shallow (the leaves' immediate parent), so this does NOT
  stress deep-LCCA; it measures per-event cost as the *active configuration grows*,
  since the exit-set computation scans the whole configuration (``algorithm.py:379``).
* **loop**  — a 2-state ping/pong chart; isolates pure per-event overhead (the
  ``WorkingMemory`` frozenset rebuild at ``algorithm.py:99``).

Run:  ``python3 bench/run_bench.py``          (full sizes, ~seconds)
      ``python3 bench/run_bench.py --quick``  (small sizes, < 5 s, for CI/smoke)
      ``python3 bench/run_bench.py --alloc``  (add tracemalloc peak-KB column)

Uses only the stdlib, matching the project's zero-dep stance (see ``run_tests.py``).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tracemalloc

# Make the package importable when run from the repo root (mirrors run_tests.py).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from statecharts import (  # noqa: E402
    initialize,
    make_chart,
    make_env,
    on,
    parallel,
    process_event,
    state,
    statechart,
)

# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------


def wide_chart(regions: int):
    """One parallel with `regions` independent ping/pong regions.

    A single ``go`` event fires a transition in *every* region simultaneously.
    """
    region_nodes = [
        state({"id": f"r{i}", "initial": f"r{i}_0"},
            state({"id": f"r{i}_0"}, on("go", f"r{i}_1")),
            state({"id": f"r{i}_1"}, on("go", f"r{i}_0")),
        )
        for i in range(regions)
    ]
    return statechart({"initial": "p"}, parallel({"id": "p"}, *region_nodes))


def deep_chart(depth: int):
    """`depth` nested compound states with two toggling leaves at the bottom."""
    node = state({"id": "leaf", "initial": "leaf0"},
        state({"id": "leaf0"}, on("go", "leaf1")),  # sibling toggle: LCCA is `leaf`
        state({"id": "leaf1"}, on("go", "leaf0")),
    )
    for i in range(depth):
        node = state({"id": f"n{i}", "initial": node.id}, node)
    return statechart({"initial": node.id}, node)


def loop_chart():
    """Minimal 2-state ping/pong — pure per-event overhead."""
    return statechart({"initial": "a"},
        state({"id": "a"}, on("go", "b")),
        state({"id": "b"}, on("go", "a")),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def drive(root, events: int, alloc: bool):
    """Send `events` ``go`` events through the functional core; return (secs, peak_kb).

    Self-check: a single warm-up event MUST change the configuration. Without this,
    a regressed builder (bad id, an event that stops matching) would make
    ``process_event`` a no-op and the harness would report a huge, meaningless
    events/sec — silently invalidating the whole benchmark (and the O(N^2) finding).
    The warm-up also removes first-call/import noise from the timed loop.
    """
    env = make_env(make_chart(root))
    wm = initialize(env)
    before = wm.configuration
    wm = process_event(env, wm, "go")  # warm-up + work-happened self-check
    if wm.configuration == before:
        raise AssertionError(
            "benchmark chart did not transition on 'go' — measuring nothing"
        )
    if alloc:
        tracemalloc.start()
    t0 = time.perf_counter()
    for _ in range(events):
        wm = process_event(env, wm, "go")
    elapsed = time.perf_counter() - t0
    peak_kb = None
    if alloc:
        peak_kb = tracemalloc.get_traced_memory()[1] / 1024.0
        tracemalloc.stop()
    return elapsed, peak_kb


# (shape name, builder-thunk, events) for full and quick modes.
#
# NOTE on the wide default: parallel-region handling is super-linear in region
# count (see `--scale` and bench/README.md — roughly O(N^2)), so N=64 takes
# minutes for a few thousand events. The default therefore uses N=32 to stay in
# the "seconds" range; run `--scale` to see the full cliff. That scaling is the
# headline finding this harness exists to surface (parent issue #2 / research #1).
def shapes(quick: bool):
    if quick:
        return [
            ("wide  (N=8 regions)",   lambda: wide_chart(8),   500),
            ("deep  (N=8 levels)",    lambda: deep_chart(8),   2_000),
            ("loop  (2 states)",      loop_chart,              5_000),
        ]
    return [
        ("wide  (N=32 regions)",  lambda: wide_chart(32),  200),
        ("deep  (N=64 levels)",   lambda: deep_chart(64),  5_000),
        ("loop  (2 states)",      loop_chart,              100_000),
    ]


def run_scale():
    """Sweep wide-chart region count to expose the parallel-handling cliff.

    Timing-only (no `--alloc` column): allocation is measured per-shape by the main
    table, and adding tracemalloc here would distort the scaling comparison."""
    print("wide-parallel scaling (500 events per point):")
    print(f"  {'N regions':>10}{'secs':>10}{'events/sec':>14}{'us/event':>12}")
    prev = None
    for n in (4, 8, 16, 32, 64):
        secs, _ = drive(wide_chart(n), 500, False)
        us = secs / 500 * 1e6
        factor = f"  (x{us / prev:.1f} vs prev)" if prev else ""
        print(f"  {n:>10}{secs:>10.3f}{500 / secs:>14,.0f}{us:>12,.1f}{factor}")
        prev = us


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="small sizes, finishes < 5 s")
    ap.add_argument("--alloc", action="store_true", help="report tracemalloc peak KB")
    ap.add_argument("--scale", action="store_true",
                    help="sweep wide-chart region count to expose the parallel cliff")
    args = ap.parse_args()

    if args.scale:
        run_scale()
        return 0

    header = f"{'shape':<22}{'events':>10}{'secs':>10}{'events/sec':>14}"
    if args.alloc:
        header += f"{'peak KB':>12}"
    print(header)
    print("-" * len(header))

    for name, build, events in shapes(args.quick):
        secs, peak_kb = drive(build(), events, args.alloc)
        rate = events / secs if secs else float("inf")
        line = f"{name:<22}{events:>10,}{secs:>10.3f}{rate:>14,.0f}"
        if args.alloc:
            line += f"{peak_kb:>12,.1f}" if peak_kb is not None else f"{'-':>12}"
        print(line)

    print("\nNote: 'wide' events each fire one transition PER region, so its raw")
    print("events/sec is not comparable to 'loop' (which does one transition per event).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
