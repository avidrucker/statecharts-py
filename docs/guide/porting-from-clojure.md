# Porting from `fulcrologic/statecharts` (Clojure) & choosing your seams

`statecharts-py` is a faithful port of Tony Kay's
[`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts). If you know that
library, this guide gets you productive fast — and, more importantly, flags the handful of
places where the two **deliberately behave differently** so they don't bite you silently.

Audience: developers coming from the Clojure library. You will assume upstream semantics
unless told otherwise — this page is where you get told otherwise.

## 1. Concept & naming map

The engines share their shape (compound/parallel/atomic/final states, history, eventless
transitions, guards, executable content) and the same protocol split. The surface names
differ:

| Upstream (Clojure) | `statecharts-py` | Notes |
|---|---|---|
| `chart/statechart` | `statechart(...)` | top-level builder |
| `state` / `parallel` / `final` / `history` | same names | builders take an opts dict + children |
| hyphenated element names (`done-data`, `auto-forward?`) | **W3C SCXML attribute names** (`donedata`, `autoforward`, `srcexpr`, `idlocation`) | we track the SCXML spelling, not the Clojure hyphenation (upstream `Conformance.adoc`) |
| protocol `DataModel` / `ExecutionModel` / `EventQueue` | same protocols | see §3 |
| lambda execution model | `CallableExecutionModel` (native callables) + an ECMAScript-subset model for the W3C suite | |
| keyword send targets (`:_parent`, `:_internal`) | W3C string forms (`"#_parent"`, `"#_internal"`, `"#_<invokeid>"`) | |

Executable content is element instances (`Script`, `Assign`, `Raise`, `Log`, `Send`,
`Cancel`); data-model ops from a `Script`/`handle` come from `ops` (e.g. `ops.assign`).

## 2. Behavioral divergences that will bite

These are the traps — corners where a chart ported verbatim behaves differently. Each is
catalogued authoritatively, with the code anchor and the test that pins it, in the
[behavior register](../reference/behavior-register.md). In short:

- **Strict block-abort is ON by default here; upstream defaults it OFF.** An error in
  executable content aborts the rest of that block. Upstream keeps the abort *opt-in*
  (`(simple/strict-env)`), per its `Conformance.adoc`.
- **System-variable writes raise `error.execution` here; upstream silently allows them.**
  Assigning to `_sessionid`/`_name`/`_event`/`_ioprocessors` is rejected (W3C §5.10);
  upstream calls this an "intentional deviation" and does not enforce it.
- **We support `binding="late"`; upstream is early-only** (upstream skips W3C test 280).
- **We implement `error.communication` and expose `_ioprocessors`; upstream does neither**
  (upstream skips 496 / 325 / 500 / 501).
- **We support native `<invoke>` including inline `content` (a **Python child statechart**
  passed to the builder); upstream supports only registry/`src`-based invocation.** So an
  inline-invoke chart that fails upstream may run here. (This is distinct from an SCXML
  *document used as a runtime data value* — W3C `test530` — which this port does **not**
  support; see [`why-98-percent-passing.md`](../../why-98-percent-passing.md).)
- **Document order:** both default to depth-first (pre-order). Upstream additionally lets
  you *choose* breadth-first when building a machine (visible only in deeply-nested parallel
  regions); this port implements depth-first only and does **not** expose that knob
  (`chart.py:271`).

See the register for the exact spec clause, `file:line`, and the failing-capable test behind
each. This guide intentionally does not restate them — the register is the single source of
truth.

## 3. The four seams

The port keeps upstream's pluggable design. You swap an implementation by passing it to
`make_env` (or `Session(chart, ...)`), or by attaching it to the `Environment`:

| Seam | Protocol | Default impl | Swap it for |
|---|---|---|---|
| Data storage | `DataModel` | `LocalDataModel` (dict) | external / normalized store (`NormalizedDataModel`) |
| Expression eval | `ExecutionModel` | `CallableExecutionModel` | a sandboxed / symbolic evaluator (see §4) |
| Event delivery | `EventQueue` | `MemoryEventQueue` + `Clock` | durable / distributed queue (`SqliteEventQueue`; Postgres for multi-node — see the [durability guide](durability.md)) |
| Step interface | `algorithm.process_event` | — | (the engine itself) |

```python
from statecharts import make_env, make_chart, Session
env = make_env(make_chart(root), data_model=MyDataModel(), event_queue=MyQueue())
s = Session(chart, env=env)
```

## 4. Escape hatch: a real scripting language behind `ExecutionModel`

The 3 non-green W3C tests are all about the *embedded scripting language*, not the engine
(see [`why-98-percent-passing.md`](../../why-98-percent-passing.md)). If you need `<script>`
flow control or inline `function(){...}` guards, you do **not** touch the engine — you drop
a real sandboxed JS evaluator (e.g. `quickjs`, `js2py`) in behind the `ExecutionModel` seam.
That seam exists precisely so the expression language is replaceable; the state-machine
algorithm is agnostic to it.

## See also

- [Behavior register](../reference/behavior-register.md) — the authoritative divergence list.
- [`why-98-percent-passing.md`](../../why-98-percent-passing.md) — the embedded-scripting scope boundary.
- Upstream [`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts) — `Conformance.adoc`, `Guide.adoc`.
