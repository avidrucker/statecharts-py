# statecharts (Python)

[![CI](https://github.com/avidrucker/statecharts-py/actions/workflows/ci.yml/badge.svg)](https://github.com/avidrucker/statecharts-py/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A faithful Python 3 port of [`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts):
**W3C SCXML** structure and semantics without the XML, expressed as plain Python data,
with swappable `DataModel` / `ExecutionModel` / `EventQueue` seams.

👉 **New here? Start with the runnable demos in [`examples/`](./examples/)** — they're
numbered in a recommended order, from the core engine to the advanced layers.

See [`init_plan.md`](./init_plan.md) for the full feasibility analysis and roadmap that motivated this port.

## Status

A working, faithful core engine. Implemented:

- Compound, parallel, atomic, and final states; the W3C processing algorithm
  (microstep/macrostep, LCCA, exit/entry sets, run-to-completion).
- Shallow and deep **history** with default transitions.
- **Eventless** (automatic) transitions and `choice` decision states.
- Guards (`cond`) and executable content: `Script`, `Assign`, `Raise`, `Log`, `Send`, `Cancel`.
- Internal vs external transitions; dotted **event-name prefix matching** (`error.network` ⊇ `error.network.timeout`).
- `done.state.*` events for compound/parallel completion; machine halts on top-level `final`.
- **Delayed events** via an injectable clock (`send_after`, `ManualClock`).
- Immutable, serializable **working memory** — the value that *is* a session (persist it between events).

Also implemented: an **SCXML XML loader** + an **ECMAScript-subset expression
evaluator**, driving the **W3C conformance suite** (see below). Plus `<if>`/`<elseif>`/
`<else>`, `<foreach>`, `<send>`/`<cancel>` with delays, `<donedata>`, early/late
binding, `error.execution`/`error.communication` semantics, system variables
(`_event`, `_sessionid`, `_name`, `_ioprocessors`), and **`<invoke>`** — synchronous
in-process child statecharts with `#_parent`/`#_<invokeid>` event routing,
`done.invoke.*`, `param`/`namelist`, `autoforward`, and `finalize`.

Higher-level layers:

- **`AsyncSession`** (`aio.py`) — an asyncio runtime that drives a session over
  real time, waking exactly when the next delayed `<send>` is due.
- **Normalized store** (`store.py`) — the Fulcro-style app-state pattern: entities
  by ident, **actors** (named idents), and **aliases** (named attribute paths), with
  `assoc_alias`/`set_actor` ops and `resolve_actors`/`resolve_aliases` helpers. The
  path toward porting Fulcro-style statechart-driven apps to Python.
- **Visualization** (`viz.py`) — `to_mermaid` and `to_dot` renderers.
- **Durable sessions** (`durable.py`) — a SQLite-backed event queue + session store,
  so a workflow can wait for hours/days **across process restarts**. Charts are
  registered by name (`ChartRegistry`); only JSON-able working memory and pending
  timers are persisted. `DurableRuntime.start`/`enqueue`/`tick` drive it. SQLite gives
  durability + safe multi-process access on one machine; the same schema ports to
  Postgres (`SELECT ... FOR UPDATE SKIP LOCKED`) for true multi-node distribution.
  See `examples/05_durable_workflow.py` for a restart-survival demo.

The scope from the original plan is now complete, including the durable event queue.

## W3C conformance

The engine runs against the real **W3C SCXML IRP** mandatory automated ecmascript
tests (vendored under `tests/w3c/cases/`, from the
[alexzhornyak/SCXML-tutorial](https://github.com/alexzhornyak/SCXML-tutorial) mirror).
A test passes by reaching `<final id="pass">`.

```
python3 tests/w3c/runner.py        # full report (add -v for INCOMPLETE/ERROR detail)
```

Current result: **153 / 156 runnable tests pass (98%)**, 0 errors. Only 3 tests are
skipped (`<script>` ecmascript bodies). The 3 remaining failures are narrow gaps:
inline `function(){...}` IIFEs in the expression evaluator, using inline SCXML as a
data *value*, and cancelling a delayed event in another live session (which the spec
itself notes is undefined).

The missing 2% is conformance of the *embedded scripting language*, not the
state-machine engine — a deliberate, reversible scope choice. See
[`why-98-percent-passing.md`](./why-98-percent-passing.md) for the full rationale.

Driving the suite surfaced (and fixed) several real bugs in the port — most
notably **document order was numbered post-order instead of pre-order**, which had
silently reversed state entry/exit ordering; `<send>` with no target was routed to
the *internal* queue instead of the *external* one; and the same transition selected
from two parallel regions executed twice (the "optimally enabled set" must be a set).

## Quick start

```python
from statecharts import Session, statechart, state, on, handle, ops

chart = statechart({"initial": "idle"},
    state({"id": "idle"}, on("start", "working")),
    state({"id": "working"},
        handle("inc", lambda env, data: [ops.assign("n", data.get("n", 0) + 1)]),
        on("done", "idle")),
)

s = Session(chart)          # initialize() runs automatically
s.send("start")
s.send("inc").send("inc")
print(s.configuration)      # frozenset({'working'})
print(s.data["n"])          # 2
```

The core is functional — `Session` just wraps it:

```python
from statecharts import make_chart, make_env, initialize, process_event
env = make_env(make_chart(chart_root))
wm = initialize(env)                       # WorkingMemory (serializable)
wm = process_event(env, wm, "start")       # -> new WorkingMemory
```

## Architecture (the four seams)

| Seam | Protocol | Default impl | Swap it for |
|---|---|---|---|
| Data storage | `DataModel` | `LocalDataModel` (dict) | external/normalized store |
| Expression eval | `ExecutionModel` | `CallableExecutionModel` | sandboxed / symbolic exprs |
| Event delivery | `EventQueue` | `MemoryEventQueue` + `Clock` | durable / distributed queue |
| Step interface | `algorithm.process_event` | — | (the engine) |

## Run it

```bash
python3 run_tests.py               # 48 tests, zero dependencies
python3 tests/w3c/runner.py        # W3C conformance report
```

### Examples

The [`examples/`](./examples/) folder has a runnable demo for every feature — each is
self-contained and needs no install. They're **numbered in a recommended reading
order** (core → structure → advanced); see [`examples/README.md`](./examples/README.md).

```bash
python3 examples/01_payment_flow.py        # core: retries, timeout, guards
python3 examples/02_visualize.py           # Mermaid / Graphviz output
python3 examples/03_load_scxml.py          # load + run SCXML XML
python3 examples/04_async_traffic_light.py # AsyncSession, real-time timers
python3 examples/05_durable_workflow.py    # SQLite durability across a restart
python3 examples/06_invoke_demo.py         # <invoke> child statechart
python3 examples/07_fulcro_store.py        # normalized store + actors/aliases
```

(When `pytest` is available, `pytest` works too — tests are standard `test_*` functions.)

## Layout

```
src/statecharts/
  elements.py        # frozen-dataclass elements (states, transitions, executable content)
  chart.py           # builder DSL + indexed Chart (id->node, parent map, document order)
  events.py          # Event + dotted-prefix matching
  algorithm.py       # the W3C SCXML algorithm (the heart)
  working_memory.py  # serializable session value
  environment.py     # bundles chart + the four protocol impls
  protocols.py       # DataModel / ExecutionModel / EventQueue (typing.Protocol)
  data_model.py / execution_model.py / event_queue.py   # default impls
  ops.py             # data-model operations (assign/delete)
  convenience.py     # on / handle / choice / send_after
  simple.py          # Session facade
  ecma.py            # ECMAScript-subset execution model (for the W3C suite)
  invocations.py     # synchronous <invoke> child-statechart sessions
  aio.py             # AsyncSession: asyncio runtime (real-time delayed sends)
  store.py           # normalized store + actors/aliases (Fulcro-style app state)
  durable.py         # SQLite durable event queue + session store + DurableRuntime
  viz.py             # to_mermaid / to_dot chart renderers
  scxml/loader.py    # SCXML XML -> element tree
tests/
  test_*.py          # native engine tests + W3C smoke guard
  w3c/runner.py      # full W3C conformance runner
  w3c/cases/         # vendored W3C mandatory ecmascript tests
```
