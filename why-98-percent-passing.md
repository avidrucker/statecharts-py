# Why W3C conformance sits at 98% (and why that's a choice, not a crack)

The engine passes **153 / 156 runnable** W3C SCXML mandatory-automated ecmascript
tests. This doc explains the 6 non-green tests — what they are, why each is left
alone, and why the remaining 2% is a deliberate, reversible scope decision rather
than a bug or an oversight.

> For the *green* corners where we deliberately diverge from the spec/upstream (often by
> being stricter), see the [behavior register](./docs/reference/behavior-register.md).

> **The one-sentence version:** 98% is the conformance of the *state-machine engine*;
> the missing 2% is conformance of the *embedded scripting language* — a different,
> much larger thing we intentionally didn't build, with a cheap escape hatch if it's
> ever needed.

## How the runner buckets tests

`tests/w3c/runner.py` sorts every test into one of five buckets. Two are not green:

| Bucket | Meaning |
|---|---|
| **PASS** | reached `<final id="pass">` |
| **FAIL** | loaded and ran, but ended in the wrong state — a real behavioral mismatch |
| **INCOMPLETE** | ran but stalled without reaching pass/fail |
| **SKIP** | the loader saw a feature we deliberately don't implement and bailed cleanly |
| **ERROR** | threw during load/run |

Current tally: **153 PASS, 3 FAIL, 0 INCOMPLETE, 3 SKIP, 0 ERROR.**

`SKIP` is an *honest "not covered"*: rather than let an unsupported-feature chart
silently FAIL (which would dishonestly imply we tried and got it wrong), the loader
raises on the unsupported construct so the pass-rate reflects what's genuinely covered.

## The crucial point first

**None of the 6 non-green tests are bugs in the statechart engine.** Every
algorithm-level defect the suite exposed *was* fixed — document order (pre- vs
post-order numbering), `<send>` target routing, transition-set de-duplication,
history, error semantics, and more. What remains lives at the **edges**: the
expression language and one spec-undefined corner.

## The 3 SKIPs — `test302`, `test303`, `test304`

All three exercise `<script>` — embedding a **block of arbitrary ECMAScript**
(variable declarations, statements, `return`) inside the chart (e.g. "a `<script>`
declares `Var1`; verify it reads back like normal data-model state").

**Why not implemented:** `ecma.py` is an ECMAScript *expression* evaluator — it
translates small expressions (`Var1 == 1`, `_event.data.x`, `In('s1')`) to Python and
evaluates them in a restricted namespace. `<script>` requires a full JS *statement*
interpreter (declarations, loops, function bodies). That is effectively "embed a
JavaScript engine" — a project an order of magnitude larger than the entire state
machine. Notably, **the original `fulcrologic/statecharts` library doesn't implement
`<script>` flow control either**, so skipping it is faithful to what we ported.

## The 3 FAILs — `test224`, `test530`, `test207`

**`test224`** — the transition guard is an inline IIFE:
`(function(str, starts){ ...; return str.slice(0, starts.length) === starts; })(Var1, Var2)`.
Same root cause as the scripts: that's a JS *function with a body*, which the
expression evaluator can't parse. The guard evaluation errors → the guard is treated
as `false` (correct SCXML behavior) → the machine lands in `fail`. Fixing it is the
same "build a real JS interpreter" task.

**`test530`** — assign an entire inline `<scxml>…</scxml>` document *as a data value*
to a variable, then `<invoke>` that variable as the child machine. This needs SCXML
documents to be first-class runtime *values* threaded through assign → expression →
invoke. Large surface, essentially never used in practice.

**`test207`** — verifies you *cannot* cancel a delayed event belonging to *another*
session. Making it pass would need a global multi-session virtual clock advancing every
session's timers in lockstep so the child's delayed events fire over simulated time —
and the SCXML spec itself notes *"there is no defined way to refer to an event in
another process."* So it tests behavior the standard leaves undefined, at a corner.

## Why leave them — the reasoning

1. **Cost vs. value is lopsided.** Each remaining test needs a disproportionately
   large feature (a JS interpreter; SCXML-as-data; a multi-session clock) to satisfy a
   corner that real statechart users essentially never touch.
2. **It's a scope boundary, not a defect.** This library is a faithful port of the
   *state-machine algorithm* plus a *pragmatic expression subset* — not, and not
   claiming to be, a complete JavaScript engine. Five of the six tests are really
   asking "did you reimplement all of JavaScript?", which is a different goal.
3. **It's reversible by design.** If `<script>` were ever needed, you would *not*
   rewrite the engine — you'd swap a real JS evaluator in behind the `ExecutionModel`
   seam (`protocols.ExecutionModel`). That seam exists precisely so the expression
   language is pluggable.

## If we ever wanted to chase them

The cheapest win by far is bundling `test224` with the three `<script>` tests: drop a
real sandboxed JS evaluator (e.g. `quickjs`/`js2py`) in behind the `ExecutionModel`
seam and ~4 tests flip at once, no engine changes. `test530` (SCXML-as-data) and
`test207` (cross-session timing) are higher-effort and lower-value — see
[`current-wont-dos.md`](./current-wont-dos.md).
