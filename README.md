# statecharts (Python)

A faithful Python 3 port of [`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts):
**W3C SCXML** structure and semantics without the XML, expressed as plain Python data,
with swappable `DataModel` / `ExecutionModel` / `EventQueue` seams.

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

Deferred (see plan): the W3C IRP **conformance test harness** (next priority),
service **invocations**/child charts, an **async**/durable event queue, the
Fulcro-style normalized-store + actors/aliases layer, and **visualization**.

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
python3 run_tests.py          # 23 tests, zero dependencies
python3 examples/payment_flow.py
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
```
