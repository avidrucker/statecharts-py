# Intended (will-do) backlog

Living list of improvements we intend to make. Companion to
[`current-wont-dos.md`](./current-wont-dos.md) (the deliberate non-goals).

## Tier 1 — high value, low effort

- [ ] **LICENSE + `py.typed`** — add an MIT `LICENSE` file (pyproject already declares
  MIT) and a `src/statecharts/py.typed` marker so downstream type-checkers use the hints.
- [ ] **Chart validation at construction** — in `Chart.__init__`, verify every transition
  `target`, `initial`, and `history_default` resolves to a real state id; raise a clear
  error (e.g. `transition in state 'a' targets unknown state 'NOPE'`) instead of a bare
  `KeyError` deep in `algorithm.py` at runtime.
- [ ] **GitHub Actions CI** — run `pytest` + the W3C conformance runner (with a pass-count
  floor) on push/PR across Python 3.10–3.12.

## Tier 2 — hardening

- [ ] **Immutability boundary** — `_Run.to_wm()` shallow-copies the datamodel
  (`dict(self.datamodel)`), so nested dicts/lists are shared between successive
  `WorkingMemory` values. Deep-copy at the boundary, switch the store to `pyrsistent`, or
  document loudly that values must be treated as flat/immutable.
- [ ] **Public serialization API** — lift `wm_to_jsonable`/`from_jsonable` out of
  `durable.py` into `WorkingMemory.to_json()/from_json()`; `durable.py` then reuses it.
- [ ] **Durable delivery robustness** — in `DurableRuntime._deliver`, a throwing
  `process_event` currently drops the already-claimed timer. Re-enqueue on failure or add
  a dead-letter table.

## Tier 3 — nice, when motivated

- [ ] **Configuration-aware visualization** — `to_mermaid(chart, wm)` that highlights the
  active states (ties viz to runtime; great for teaching/debugging).
- [ ] `ruff` + `mypy` configs (code already sprinkles `# noqa` anticipating them).
- [ ] `CHANGELOG.md` and a short `CONTRIBUTING.md`.

## Deeper / optional

- [ ] Persistent/immutable data via `pyrsistent` for true value-semantics on nested data.
- [ ] Persist active `<invoke>` children in durable mode (today they're in-process only).
- [ ] Auto-generated API docs site (e.g. `pdoc`).
- [ ] A concrete **Postgres** durable backend (the `EventQueue` seam + SQLite schema
  already port; `SELECT ... FOR UPDATE SKIP LOCKED` for multi-node).
