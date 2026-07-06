# Benchmarks

Zero-dependency throughput harness for the statecharts engine. Establishes a baseline
so future changes have something to diff against, and surfaces where the engine scales
badly. See parent issue [#2](https://github.com/avidrucker/statecharts-py/issues/2) and
research [#1](../docs/research/improvement-areas.md) (RQ1).

## Running

```bash
python3 bench/run_bench.py           # full sizes, ~6 s
python3 bench/run_bench.py --quick   # small sizes, < 1 s (CI/smoke)
python3 bench/run_bench.py --alloc   # add a tracemalloc peak-KB column
python3 bench/run_bench.py --scale   # sweep wide-parallel region count (the cliff)
```

Uses only the stdlib (`time.perf_counter`, `tracemalloc`), matching the project's
zero-dependency stance (cf. `run_tests.py`).

## The three shapes

| Shape | What it stresses |
|---|---|
| **wide** | one `parallel` with N ping/pong regions — every event fires one transition **per region**, stressing the optimal-enabled-set / LCCA work across parallel regions |
| **deep** | N nested compound states with two toggling leaves — LCCA computed up a deep tree |
| **loop** | a 2-state ping/pong chart — pure per-event overhead (the `WorkingMemory` frozenset rebuild) |

> `wide` events each fire N transitions, so its raw events/sec is **not** comparable to
> `loop` (one transition per event). Compare a shape only against itself over time.

## Baseline (author's machine — Python 3, default run)

Numbers are indicative, not a pass/fail gate — they exist to be diffed against.

```
shape                     events      secs    events/sec
--------------------------------------------------------
wide  (N=32 regions)         200     1.590           126
deep  (N=64 levels)        5,000     1.514         3,302
loop  (2 states)         100,000     2.924        34,194
```

## Headline finding: parallel handling is super-linear in region count

`--scale` sweeps the wide chart's region count at a fixed 500 events:

```
 N regions      secs    events/sec    us/event
         4     0.061         8,213       121.8
         8     0.177         2,820       354.7   (x2.9 vs prev)
        16     0.777           644     1,553.6   (x4.4 vs prev)
        32     4.039           124     8,077.5   (x5.2 vs prev)
        64    34.749            14    69,498.3   (x8.6 vs prev)
```

Each doubling of region count multiplies per-event cost by **4–9×** — clearly worse than
O(N). Per-event work in a parallel machine grows roughly **O(N²)** (or worse) in the
number of regions. Deep nesting, by contrast, scales ~linearly.

This is the concrete, measured justification for pursuing the per-event optimization work
(the "opportunity #6" in research #1): most likely the optimal-enabled-set computation
and/or the per-event `WorkingMemory` frozenset rebuild (`src/statecharts/algorithm.py:99`)
being re-done across all regions each microstep, with no cross-step memoization — the same
class of problem XState fixed by caching transition lookups
([xstate#3757](https://github.com/statelyai/xstate/discussions/3757)).

**Recommendation:** file a DEV ticket to profile and address the parallel-region cliff,
using `--scale` as the before/after measure. Do not optimize blind — this table is the
target to beat.
