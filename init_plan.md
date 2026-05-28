# Research + Plan: A Python 3 port of `fulcrologic/statecharts`

## Context

You want to investigate porting [`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts) — a
Clojure(Script) statechart engine that implements W3C **SCXML** structure and semantics *without* the XML — to
Python 3. Your stated goals are simultaneously (a) a learning exercise, (b) a reusable Python library, (c) a path
to port Fulcro-style statechart-driven app patterns into Python, and (d) a feasibility evaluation. Target fidelity:
a **faithful port of the W3C SCXML algorithm and the protocol abstractions**, deferring **visualization** and the
**async/core.async event loop** for now.

This document answers your three questions — *why useful*, *benefits/use-cases*, *costs/difficulties* — and then
lays out a concrete, phased implementation plan with a verification strategy.

---

## 1. What the source library actually is (so the port target is clear)

The Fulcrologic library is unusual and worth porting *because* of how it's built, not just what it does:

- **Treats the chart and all executable content as plain data** (EDN), not code. A chart is a nested data
  structure of elements (`state`, `transition`, `parallel`, `history`, `final`, `on-entry/on-exit`, `script`,
  `raise`, `send`, `assign`, `log`, `invoke`, `data-model`). This is what makes charts serializable, versionable,
  and inspectable.
- **Closely follows the W3C SCXML processing algorithm** (the normative pseudo-code: `microstep`,
  `selectTransitions`, `exitStates`, `enterStates`, `computeExitSet`, LCCA computation, the event-processing
  loop). The internal implementation deliberately uses mutable cursors (volatiles) over an otherwise functional
  core to match the imperative spec.
- **Built around four swappable protocol abstractions** rather than one monolith:
  - `DataModel` — how session data is stored/read/written (local map, or external store).
  - `ExecutionModel` — how guards/actions are *interpreted* (could be Clojure fns, SCI, JS, etc.).
  - `EventQueue` — how events are queued/delivered (in-memory, durable, distributed).
  - the **processing algorithm** itself, plus extensible **executable-content elements via multimethods**.
- **Functional, stateless step interface**: you hand it working memory + an event, it returns *new* working
  memory. Working memory (configuration, history, internal data) is a serializable value. This is what enables
  **long-lived, durable sessions** tied to entities (the canonical example: a money-transfer that lives for days).
- Modules (from the repo): `chart`, `elements`, `protocols`, `environment`, `events`, `runtime`, plus
  `algorithms/`, `data_model/`, `execution_model/`, `event_queue/`, `working_memory_store/`, `invocation/`,
  `registry/`, `integration/` (Fulcro), `visualization/`, `convenience`, `testing`, `simple`, and `specs`/`malli`.

Roughly: a few thousand lines of dense algorithmic Clojure. The *core engine* (algorithm + elements + protocols +
data model + working memory + events) is the porting target; integrations/viz/async are peripheral.

---

## 2. Why a Python port is useful — benefits & use-cases

### The gap it fills in the Python ecosystem
The Python landscape has state-machine libraries, but **none combine all of**: faithful SCXML semantics +
data-as-structure (serializable charts) + swappable data/execution/queue backends + durable long-lived sessions.

| Library | Hierarchical | Parallel | History | SCXML algo | Data-as-structure | Durable sessions | Pluggable backends |
|---|---|---|---|---|---|---|---|
| `transitions` | via extension | partial | no | no | no (code-defined) | no | no |
| `python-statemachine` | limited | no | no | no | no | no | no |
| **Sismic** | yes | yes | yes | *based on* SCXML (with deviations) | YAML def, **Python `eval` for guards/actions** | no (sync sim) | limited |
| `miros` / `pyscm` / `hat.stc` | yes | some | some | partial | partial | no | no |
| **This port (goal)** | yes | yes | yes | **faithful** | **yes (data, not eval'd strings)** | **yes** | **yes (protocols)** |

The closest existing library is **Sismic** (mature, well-documented, great testing story incl. design-by-contract
and BDD). But Sismic embeds **Python code as strings** evaluated in a context, and is oriented toward synchronous
*simulation/testing* rather than durable distributed execution. The Fulcrologic design's distinctive value —
**charts and actions as inspectable data + pluggable persistence/execution** — is genuinely missing in Python.

### Concrete benefits / use-cases
- **Durable backend workflows**: order/payment/transfer lifecycles, onboarding flows, approval pipelines that
  persist for hours/days. Working memory is a serializable value → store it in Postgres/Redis between events.
- **Agent / LLM orchestration**: a statechart is an excellent controller for multi-step agent flows (guards on
  tool results, parallel sub-tasks, retries-with-count, timeouts via delayed events). Data-as-structure means an
  LLM can *read and even propose* chart fragments.
- **Game / simulation / IoT / robotics logic**: the SCXML algorithm was designed for exactly this (parallel
  regions, history, eventless transitions).
- **UI / app state management**: the path toward porting Fulcro-style patterns — a normalized data model with
  "actors" and "aliases" driven by a chart — into Python web stacks (FastAPI + a frontend, Textual TUIs, etc.).
- **Interoperability / portability**: because charts are data, the *same* chart definition could in principle be
  shared across the Clojure and Python implementations (modulo expression language).
- **Learning value (high)**: implementing the W3C algorithm faithfully forces deep understanding of LCCA,
  exit/entry sets, microstep/macrostep, and the difference between internal and external events — knowledge that
  transfers directly back to using the Clojure library more expertly.

---

## 3. Costs, difficulties & risk predictions

These are the things that will actually bite. Ordered by expected pain.

1. **Immutability mismatch (the central tension).** Clojure's persistent data structures make "return new working
   memory" cheap and safe. Python has no built-in persistent collections. Options, in order of recommendation:
   - Treat working memory as an *immutable-by-convention* value: use `dataclasses` (`frozen=True`) + `pyrsistent`
     (or `immutables.Map`) for the data map and `frozenset` for the configuration. **Recommended.**
   - Or copy-on-write with `copy.deepcopy` at step boundaries (simple, slower, fine to start).
   - Risk: subtle aliasing bugs if you allow in-place mutation of nested data. Mitigation: keep the spec's
     volatile cursors *internal to one step* only, and return a fresh value at the boundary — mirror the
     library's own discipline.

2. **The W3C algorithm is fiddly and order-sensitive.** Document order, LCCA computation, the exit/entry set
   construction, history restoration, and the eventless-transition fixpoint loop are easy to get *almost* right.
   Off-by-one in ordering produces charts that work 95% of the time. Mitigation: port the spec pseudo-code
   *literally* first (readability over Pythonic cleverness), then refactor. Use the **W3C IRP/SCXML conformance
   test suite** as ground truth (see verification).

3. **Multimethods → registries.** Clojure's `defmulti`/`defmethod` (open dispatch on element type) has no direct
   Python equivalent. Use a **registry dict** keyed by element `type`, or `functools.singledispatch` on element
   classes. Straightforward, but you lose the open-extension elegance unless you design the registry deliberately.

4. **Protocols → ABCs / `typing.Protocol`.** Clean mapping: `DataModel`, `ExecutionModel`, `EventQueue`,
   `WorkingMemoryStore` become `typing.Protocol` (structural) or `abc.ABC` (nominal). Low risk; mostly design taste.

5. **ExecutionModel: how do guards/actions get expressed in Python?** The Clojure version stores fns. In Python,
   the cleanest faithful analog is: actions/guards are **plain Python callables** `(env, data) -> ops | bool`
   stored in the (in-memory) chart. This keeps charts as data structures of *callables* (not eval'd strings —
   better than Sismic). For the *serializable* story you'll later need a symbolic/registry-of-named-fns layer.
   This is a genuine design fork — note it but don't over-build early.

6. **EDN / keyword ergonomics.** Clojure keywords (`:state/id`) and EDN literals are pervasive. Python has no
   keyword type; use `str` or `enum`/`Enum` for state ids and plain dicts for data. The dotted **event-name prefix
   matching** (`error.network.timeout` matched by `error.network` and `error`) must be reimplemented explicitly.

7. **`core.async` event loop & invocations.** Deferred per your scope. The async I/O processor, delayed-event
   timers, and child-statechart invocation lean on core.async / futures. Defer the loop; but design the
   `EventQueue` protocol now so a sync in-memory queue works and an async one can slot in later. Delayed events
   (`send-after`) need *some* clock abstraction even in sync mode — provide an injectable clock (like Sismic does).

8. **No host framework to integrate with.** Fulcro integration (actors/aliases, normalized state map, remote
   mutations) is Clojure/Fulcro-specific and has **no Python equivalent**. The "port Fulcro-style apps" goal is
   really "reimplement the *pattern*" (normalized store + actor/alias indirection) against a Python store — that's
   a *second project* on top of the engine, not part of the core port.

9. **`spec`/`malli` validation.** Replace with `pydantic` or `dataclasses` + hand-written validators, or skip
   validation initially and add it once element shapes stabilize.

10. **Performance.** Python is far slower than the JVM for the tight algorithm loop. For typical workflow/UI charts
    this is irrelevant. Only matters for very high event throughput; not a v1 concern.

### Effort prediction
- **Core engine MVP** (states/transitions/parallel/history/final, data model, in-memory event queue, microstep +
  macrostep loop, entry/exit/guard/assign/raise, event-prefix matching): a focused build — the algorithm is ~1
  well-understood file plus supporting modules.
- **W3C conformance** (passing a meaningful subset of the IRP test suite): the long tail; this is where "faithful"
  earns its cost. Expect the test suite to surface several ordering/history bugs.
- **Invocations + durable/async queue + Fulcro-pattern layer**: each is a separable later phase.

---

## 4. Recommended approach & phased plan

Build a faithful, *synchronous* core engine with the four protocol seams in place from day one, verify it against
the W3C test suite, then layer extras. Package name suggestion: `statecharts` (PyPI-style src layout).

### Proposed module layout (mirrors the original, Pythonized)
```
statecharts-py/
  pyproject.toml
  src/statecharts/
    __init__.py
    protocols.py        # DataModel, ExecutionModel, EventQueue, WorkingMemoryStore (typing.Protocol/ABC)
    elements.py         # State, Transition, Parallel, History, Final, OnEntry/OnExit, Script, Assign, Raise, Send, Log, DataModel-element  (frozen dataclasses)
    chart.py            # Chart construction + indexing (id->element, document order, parent/child, descendants)
    events.py           # Event type + dotted-prefix matching
    environment.py      # env assembly: bundles data-model/exec-model/event-queue/working-memory instances
    working_memory.py    # configuration (frozenset), history map, internal data; immutable-by-convention
    algorithm.py         # THE W3C algorithm: process_event, microstep, macrostep, select_transitions,
                         #   exit_states, enter_states, compute_(exit|entry)_set, LCCA, history handling
    data_model/local.py  # in-memory DataModel + ops (assign, etc.)
    execution_model/callable.py  # guards/actions as (env,data)->... Python callables
    event_queue/memory.py        # synchronous in-memory FIFO + injectable clock for delayed sends
    convenience.py       # on(), handle(), choice(), send_after() helpers
    registry.py          # element-type dispatch (executable content)
  tests/
    test_algorithm.py
    test_history.py, test_parallel.py, ...
    w3c/                 # harness that runs W3C SCXML IRP tests (txml/scxml) against the engine
```

### Phases
1. **Spike / skeleton** — `pyproject.toml`, package skeleton, `protocols.py`, `elements.py` (frozen dataclasses),
   `events.py` with dotted-prefix matching. A trivial 2-state chart that does one transition. Establishes the
   immutable-working-memory convention and the env-assembly pattern.
2. **Core algorithm (atomic + compound + transitions)** — port `process_event`, `microstep`, `select_transitions`,
   `exit_states`/`enter_states`, LCCA, document-order helpers. On-entry/on-exit, `assign`, `raise`, internal-event
   loop. Verify with a traffic-light / toggle chart.
3. **Parallel + history + final + eventless transitions + done.events** — the hard SCXML bits. This is where W3C
   tests start mattering.
4. **W3C conformance harness** — load the W3C SCXML IRP test charts (they're XML; write a small XML→element-tree
   loader *just for tests*, or hand-translate a subset), run them, track pass rate. This is the verification spine.
5. **Delayed events + clock** — `send` with `delay`, `send_after`, cancel; injectable clock (real vs simulated, à
   la Sismic) so it's testable without real time. Still synchronous.
6. **Convenience + ergonomics + validation** — `on/handle/choice/send_after`, optional `pydantic` validation,
   a `simple`-style facade and a `testing` helper (`in_state?`, `run_events`, `goto_configuration`).
7. **(Deferred, separate)** invocations/child charts, async/durable event queue, the Fulcro-style normalized-store
   + actors/aliases layer, visualization (DOT/Mermaid export).

### Design decisions locked in by your answers
- **Faithful**: port the spec pseudo-code literally; keep the four protocol seams; preserve data-as-structure.
- **Skip for now**: visualization, async/core.async event loop (but design `EventQueue` so async can slot in).
- **Immutability**: `pyrsistent`/`immutables` + frozen dataclasses + `frozenset` configuration (decide in Phase 1;
  could start with `deepcopy` and swap later).
- **Guards/actions**: Python callables stored in the chart (better than string-eval); symbolic/serializable layer
  is a later concern.

---

## 5. Verification strategy

- **Unit tests per construct** (`pytest`): atomic transitions, compound entry-of-initial, parallel fan-out/join,
  shallow vs deep history, eventless-transition fixpoint, `done.state.*` / `done.invoke` events, dotted event
  prefix matching, guard ordering, internal-vs-external event precedence.
- **W3C SCXML IRP conformance suite as ground truth** — the normative manual/automated test set
  (`mandatory`/`optional` `txml`/`scxml` tests). A passing subset is the real proof of "faithful." Track and report
  pass rate; let failures drive bug-finding (this is exactly how Sismic validated its semantics).
- **Cross-check against the Clojure library** for a handful of non-trivial charts: run the same chart + event
  sequence in both and assert identical resulting configuration + data. High-signal regression check.
- **Property/scenario tests** (optional, later): given a chart, no two parallel siblings exit each other; the
  configuration is always a valid state tree; etc.
- **Run it**: `pip install -e .` then `pytest`; a small `examples/` script (e.g. a payment-flow chart) executed
  step-by-step printing configuration after each event to eyeball behavior.

---

## Open follow-ups (not blockers)
- Decide the immutability library in Phase 1 (`pyrsistent` vs `immutables` vs `deepcopy`-to-start).
- Decide how much of the W3C XML test loader to build vs hand-translate (affects Phase 4 size).
- The Fulcro-pattern port and serializable-expression layer are deliberately scoped *out* of the core; revisit
  once the engine passes conformance.

## Sources
- fulcrologic/statecharts — https://github.com/fulcrologic/statecharts
- Sismic — https://sismic.readthedocs.io/  · paper: https://www.researchgate.net/publication/347707426
- W3C SCXML spec (incl. algorithm + test suite) — https://www.w3.org/TR/scxml/
- hat.stc — https://hat-stc.hat-open.com/  · pyscm — https://github.com/zen747/pyscm  · miros — https://aleph2c.github.io/miros/
- statecharts.dev resources — https://statecharts.dev/resources.html
