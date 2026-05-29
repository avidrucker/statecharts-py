# Current non-goals (won't-do, for now)

Things we are deliberately **not** doing, with the reasoning, so the decision is recorded
rather than re-litigated. Companion to [`intended-will-dos.md`](./intended-will-dos.md).

## The 3 remaining W3C conformance failures

Conformance sits at 153/156 runnable (98%). The last three are low-ROI and partly
spec-undefined; not worth the complexity:

- **test224** — inline `function(){...}` IIFE in an expression. Would require a real JS
  parser/interpreter in `ecma.py`; the evaluator is intentionally a *subset*.
- **test530** — using inline SCXML as a *data value* (assign an `<scxml>` to a var, then
  `<invoke>` it). Niche; large surface for little gain.
- **test207** — cancelling a delayed event in *another* live session. The SCXML spec
  itself notes there's no defined way to refer to an event in another process.

## Performance optimization

The algorithm recomputes ancestor/descendant queries and document-order sorts per step.
Fine for any realistic chart. No optimization without a measured need.

## PyPI publishing

Premature at `0.1.0`. Revisit once the API is stable and we actually want others to
`pip install` it.

## Full distributed (multi-node) durable backend, now

SQLite gives durability + safe multi-process on one machine, which covers local/testing
needs. A concrete Postgres backend is a *will-do* (see the companion file) but not needed
yet — the seam and schema already make it a drop-in later.
