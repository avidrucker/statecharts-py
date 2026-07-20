# Codebase quality audit — code review of statecharts-py (#37)

A consolidated quality audit of `statecharts-py`, tracked by **#37**. Reviewed against three
lenses the maintainer asked about — **organization**, **performance**, **testing** — plus
**correctness** and **security**. The review fanned out across five areas (core engine,
durability, SCXML/ecma/invoke, peripheral layers + API, test suite); the sharpest findings
were then independently reproduced.

Scope: `src/statecharts/**`, `tests/**`, `run_tests.py`, `tests/w3c/runner.py`,
`bench/**`, `.github/workflows/ci.yml`. No source behaviour was changed — this is an audit.
Fixes land under their own tickets (see [Recommended follow-up tickets](#recommended-follow-up-tickets)).

> **Status since audit (2026-07-19).** Finding **§1.1** (datamodel re-init on entry) has since
> been fixed under **#38** — `dm_initialized` is now persisted across both the in-memory and the
> durable (SqliteStore/Postgres) boundaries, with red-green regression tests; its late-binding
> companion guard is tracked in **#39**. The verify-claims claims ledger this audit draws on now
> lives in the repo config via **#40**. Every other finding below stands as reviewed on `0c7776f`;
> the [follow-up table](#recommended-follow-up-tickets) marks what has shipped.

## How to read this

- **Landmarks, not line numbers** (RULES H1): every finding names a `function`/`class` + file.
- **Confidence** — `CONFIRMED` = reproduced in-session with a command/red case (RULES G3);
  `PLAUSIBLE` = grounded in code read directly, not independently re-executed.
- **Severity** is review severity (High / Medium / Low), not a `severity:*` defect label
  (which RULES D1 reserves for filed bugs). High = correctness/data-integrity/security.

## Executive summary

The **functional core and the SQLite durable backend are genuinely strong** — semantically
faithful, and `test_durable.py` is exemplary (real simulated crashes, per-event atomicity,
poison/backoff/gate all pinned by red-green tests). The green checkmark is trustworthy *for
the core, SQLite durability, and the 153-case W3C surface.*

Five items warrant blocking attention:

1. **Datamodel re-initialises on state entry** — silent data corruption (H, CONFIRMED).
2. **ecma evaluator is an RCE sandbox escape** — `eval` behind an empty-`__builtins__` guard,
   bypassed to run `os.getcwd()` in this repo (H, CONFIRMED).
3. **SCXML loader is vulnerable to billion-laughs entity-expansion DoS** (H, CONFIRMED;
   external-entity XXE is *not* exposed).
4. **The entire Postgres backend is un-run behind the merge gate** — 312 LOC / 11 tests that
   no-op in CI (H, CONFIRMED).
5. **AsyncSession hangs with any non-default EventQueue** — a leaky seam defeats delayed sends
   (H, CONFIRMED seam-break; hang is the direct consequence).

Security items #2 and #3 are acceptable **only** if SCXML input is fully trusted — a trust
boundary currently undocumented and unenforced.

---

## 1. Core engine
`algorithm.py`, `chart.py`, `elements.py`, `events.py`, `working_memory.py`

### 1.1 — HIGH · correctness · CONFIRMED — datamodel re-initialised on entry
**Landmark:** `_Run.__init__` (seeds `dm_initialized = set(wm.configuration)`) and
`_enter_states` (`if node.datamodel and sid not in run.dm_initialized: _apply_datamodel`) in
`algorithm.py`; `WorkingMemory` in `working_memory.py`.

`dm_initialized` — the "have I already applied this state's `<data>`?" guard — is **not** a
field of `WorkingMemory`. It is rebuilt as `set(wm.configuration)` on every `_Run`, so it is
forgotten across every `process_event`. Any state entered *after* the initial macrostep has
its datamodel re-applied on entry, clobbering values changed while the state was inactive.
This breaks both binding modes: early binding must initialise once at document start; late
binding must initialise on *first* entry only.

**Reproduced** (early binding): a var `v` inits to `1`, is set to `5` while its owning state is
inactive, then resets to `1` on entry — and again on every re-entry:
```
start        v = 1     (early binding => 1 at doc init)
after setv   v = 5     (expect 5)
after enter B v = 1    (BUG: re-init on entry)
re-enter B   v = 1     (BUG: re-init again)
```
**Fix:** persist `dm_initialized` (or an "ever-entered" set) in `WorkingMemory` and thread it
through `to_wm`/`_Run.__init__`. Add regressions: modify an early-bound var before entry; set
a late-bound var, exit, re-enter. Neither is covered today (`test_semantics.py`'s
`test_late_binding_defers_data_until_entry` only exercises a single entry).

### 1.2 — MEDIUM · performance · PLAUSIBLE — transition domain recomputed 3× per microstep
**Landmark:** `_transition_domain` / `_effective_target_states`, called from
`_remove_conflicting_transitions`, `_compute_exit_set`, and `_compute_entry_set` in
`algorithm.py`.

For each selected transition the domain + effective targets (an LCCA ancestor walk) are
recomputed in all three phases per microstep. **Fix:** compute once per enabled transition at
selection time, memoise on the run cursor, reuse across exit/entry.

### 1.3 — MEDIUM · performance · PLAUSIBLE — `child_states` rebuilds a tuple on every access
**Landmark:** `StateNode.child_states` (`elements.py`), reached via
`Chart.is_atomic`/`is_compound` (`chart.py`), which `_select_transitions` calls for every
configuration member on every event; `is_atomic` also calls `self.node(sid)` twice.

The tree is frozen at index time, yet the atomic/compound predicate reallocates per call on the
hottest path. **Fix:** precompute `atomic`/`compound`/`child_state_ids` sets in `Chart.__init__`
(alongside `doc_order`); make the predicates O(1) lookups.

### 1.4 — LOW · organization · PLAUSIBLE — `_select_transitions` / `_select_eventless_transitions` duplicated
**Landmark:** the two functions in `algorithm.py` are identical except the per-transition
predicate. **Fix:** one `_select(run, predicate)` helper.

### 1.5 — LOW · correctness · CONFIRMED (static) — `Dict` used but not imported
**Landmark:** `_active_children_index` / `_active_descendants` annotations in `algorithm.py`;
the typing import lacks `Dict`. Survives only because `from __future__ import annotations`
makes annotations lazy strings; `typing.get_type_hints()` on the module would `NameError`.
**Fix:** add `Dict` to the import.

### 1.6 — LOW · organization · PLAUSIBLE — frozen `WorkingMemory` wraps mutable dicts
**Landmark:** `WorkingMemory` (`working_memory.py`) — a frozen dataclass holding mutable
`datamodel`/`invocations`/`history_value` dicts and (per its own comment) live child sessions,
so it is neither immutable nor fully serializable despite the "serializable value that *is* a
session" docstring. **Fix:** freeze the contents or soften the docstring.

**Clean:** `events.py` (prefix/wildcard/multi-descriptor matching, all covered by
`test_events.py`); the `_remove_conflicting_transitions` preemption rewrite (covered by
`test_parallel.py`); the `elements.py`/`chart.py` builder DSL.

---

## 2. Durability
`durable.py` (SQLite), `durable_postgres.py` (Postgres)

Two known gaps are already filed: **#35** (Postgres per-session FIFO across workers) and
**#36** (Postgres dead-letter cap). Not re-filed here; adjacent findings below.

### 2.1 — HIGH · correctness · PLAUSIBLE — same-session event overtakes a poison sibling, single worker
**Landmark:** `PostgresRuntime.tick` poison branch + `PostgresStore.claim`
(`WHERE status='ready' OR (in_flight AND claimed_at <= now - lease)`) in `durable_postgres.py`.

Session `s` has `E1(due=0)` then `E2(due=0)`, one worker. `tick` claims `E1`, delivery raises,
the `except` leaves `E1` in-flight with a live lease and `continue`s; the next iteration's
`claim` skips the still-leased `E1` and claims/delivers `E2` — **E2 lands before E1 in the same
tick, no second worker needed.** This is *worse* than the documented "no per-session ordering
across workers" caveat, and untested. SQLite fixed exactly this with the #26 retry-gate.
**Fix:** claim-by-`session_id` / a session gate; folds into #35's design.

### 2.2 — MEDIUM · correctness/liveness · PLAUSIBLE — `next_due` can strand crashed-worker recovery
**Landmark:** `PostgresStore.next_due` (`SELECT MIN(due) WHERE status='ready'`) in
`durable_postgres.py`. In-flight rows are excluded, so a crashed-worker row that only a
lease-expiry reclaim can resurface is invisible; if it is the only pending work, `next_due()`
returns `None` and a poller mirroring the SQLite `sleep-until-next_due` idiom parks forever.
**Fix:** fold the soonest `claimed_at + lease` of in-flight rows into `next_due`, or document
that Postgres pollers must tick unconditionally on an interval ≤ lease.

### 2.3 — MEDIUM · testing · PLAUSIBLE — the Postgres `tick` claim-loop is not exercised red-green
**Landmark:** `test_pg_at_least_once_can_duplicate_side_effects` drives `store.claim` +
`rt._deliver` directly, bypassing `PostgresRuntime.tick`;
`test_pg_crash_between_claim_and_persist_redelivers` uses a *healthy* handler, not a failing
one. The production poison → leave-in-flight → lease-expiry-reclaim path is asserted only in
prose. **Fix:** a test that drives `tick` with a failing handler and asserts redelivery after
lease expiry. (See also §5.1 — these tests don't even run in CI.)

### 2.4 — MEDIUM · organization · PLAUSIBLE — SQLite/Postgres store duplication
**Landmark:** `_exec`/`_query_one`/`_query_all` and the CRUD set (`save_session`,
`load_session`, `session_ids`, `enqueue`, `cancel`) are copy-pasted between `SqliteStore` and
`PostgresStore`, differing only in paramstyle (`?` vs `%s`) and caught driver-error type. The
(de)serialization helpers are already shared, proving the seam exists. **Fix:** extract a
`_Store` base with a paramstyle placeholder + a `_driver_error` hook so CRUD fixes land once.

### 2.5 — MEDIUM · performance · PLAUSIBLE — 4 commits per delivered event on Postgres
**Landmark:** `PostgresRuntime.tick`/`_deliver` issue `claim`, `load_session`, `save_session`,
`delete_timer` as four autocommit statements; SQLite batches the equivalent into one
`atomic()`. A drain of K events = 4K round-trips. **Fix:** share one transaction for
load/save/delete (the statement-scoped `claim` lock still needs its own).

### 2.6 — LOW/MEDIUM · correctness · PLAUSIBLE — undecodable payload loops forever
**Landmark:** `PostgresRuntime.tick` — a row whose `json.loads`/`event_from_jsonable` fails
hits the poison branch and is re-claimed every lease interval **forever** (it can never
succeed). SQLite dead-letters an undecodable payload immediately. Qualitatively worse than the
transient poison the docs describe; fold into #36.

### 2.7 — LOW · organization · PLAUSIBLE — vestigial Postgres surface
**Landmark:** `PostgresRuntime` inherits `DEAD_LETTER_CAP`, `BACKOFF_*`, `_park_or_backoff`,
gate-aware `next_due` — none apply — so `PostgresRuntime.DEAD_LETTER_CAP == 5` reads as a live
cap that does not exist; the `attempts` column is `RETURNING`-ed by the claim SQL but never
incremented. Intentional (reserved for RQ4) but misleading. **Fix:** override/annotate until
#35/#36 land.

### 2.8 — LOW · performance · PLAUSIBLE — SQLite `timers_due` index misses the `id` tiebreak
**Landmark:** `peek_one_due` orders by `t.due, t.id` but `_SCHEMA` creates `timers_due ON
timers(due)`; the `(due, id)` composite (already applied to Postgres as `timers_due_ready`) was
not back-ported. **Fix:** make it `(due, id)`.

**Clean / accepted:** `DurableRuntime` (SQLite) exactly-once, atomicity, poison classification,
and #26 FIFO gate are each pinned by real tests. The cascade-`<send>` `StoreError` →
`error.execution` seam (SCP-C-035) is documented and tested — an accepted trade-off, but the one
place a transient infra failure drops an internal event rather than retrying; worth louder framing.

---

## 3. SCXML / ecma / invoke
`scxml/loader.py`, `ecma.py`, `invocations.py`

### 3.1 — HIGH · security · CONFIRMED — ecma `js_eval` is an RCE sandbox escape
**Landmark:** `js_eval` (`ecma.py`) runs `eval(compile(tree, ...), {"__builtins__": {}}, ns)`
on an attacker-influenced string. Emptying `__builtins__` is the textbook-broken sandbox —
nothing restricts attribute access or comprehensions. **Reproduced against this repo:**
```python
js_eval("[c for c in ().__class__.__base__.__subclasses__() "
        "if c.__name__=='catch_warnings'][0]()._module."
        "__builtins__['__import__']('os').getcwd()", {})
# -> '/home/avi/Documents/Study/Python/statecharts-py'
```
Any SCXML `cond`/`expr`/`<data>` string is a full RCE vector. **Fix:** do not `eval`. Walk the
parsed AST with a strict node-type allowlist and **reject any `Attribute` access to dunder
names**; add a red test asserting `().__class__` raises `EcmaError`.

### 3.2 — HIGH · security · CONFIRMED — loader vulnerable to billion-laughs DoS (XXE is not)
**Landmark:** `load_string`/`load_file` (`loader.py`) use bare
`xml.etree.ElementTree.fromstring`. **Reproduced:** a nested-entity document expanded a small
input to 10,000+ chars (scales exponentially → DoS). **Confirmed negative:** external-entity
XXE is *not* exposed — a `file:///` SYSTEM entity raised `ParseError` (ElementTree does not
resolve external entities), so local-file disclosure/SSRF is not a risk. **Fix (zero-dep):**
reject documents containing a `<!DOCTYPE>`/`<!ENTITY>`, or install a custom expat parser that
rejects entity declarations; document the trust boundary either way.

### 3.3 — MEDIUM · correctness · CONFIRMED — `js_to_py` corrupts string literals
**Landmark:** `js_to_py` (`ecma.py`) does textual substitution (`===`,`!==`,`&&`,`||`,`!`,
`typeof`) on raw source *before* parsing, so string content is mangled. **Reproduced:**
```python
js_to_py('x == "a&&b!c"')  # -> 'x == "a and b not c"'
```
Any `expr`/`log` string containing those tokens silently changes value. **Fix:** rewrite on AST
nodes (BoolOp/UnaryOp/Compare) after `ast.parse`, not on source text.

### 3.4 — MEDIUM · testing · PLAUSIBLE — the 3 known W3C failures are silently absent, not xfailed
**Landmark:** `test_w3c_representative_subset` (`test_w3c_smoke.py`) asserts only a 21-entry
passing subset; `runner.run_all` is print-only. The known failures (inline-IIFE, inline-SCXML-
as-data, cross-session-cancel) are neither xfail nor gated, so a PASS→FAIL regression outside
the 21 is invisible. **Fix:** an explicit `KNOWN_FAILURES` set + a test asserting the full-suite
PASS count equals the floor and exactly those ids fail. (See §5.2.)

### 3.5 — MEDIUM · testing/organization · PLAUSIBLE — untested / wrong ecma builtins
**Landmark:** `_base_namespace`/`JSArray` in `ecma.py` — no W3C case exercises `parseInt`,
`parseFloat`, `Boolean`, `indexOf`, `join`, `push`, `Infinity`, `NaN`; and `parseInt`
(`lambda x,*a: int(x)`) ignores radix and raises on `"3.5"`/`"0x1F"`. **Fix:** add native unit
tests per supported builtin, or delete the unused ones to shrink the trusted surface.

### 3.6 — LOW · performance · PLAUSIBLE — `js_eval` recompiles every call
**Landmark:** `js_eval` rebuilds the namespace, re-runs `js_to_py`/`ast.parse`/transform/
`compile` for the same `cond`/`expr` string on every macrostep/foreach iteration. **Fix:**
`lru_cache` the compiled code object keyed by source; bind variables per call.

### 3.7 — LOW · organization · PLAUSIBLE — duplicated param-parsing / narrow inline-invoke
**Landmark:** the `(child.get("name"), child.get("expr") or child.get("location"))` idiom is
copy-pasted across `_parse_send`, `_make_donedata`, `_parse_invoke`; and `_parse_invoke` only
recognises a nested `<scxml>` as inline content, silently dropping other `<content>` forms.
**Fix:** a `_parse_params(el)` helper; raise `UnsupportedConstruct` on unrecognised inline content.

---

## 4. Peripheral layers + public API
`store.py`, `viz.py`, `aio.py`, `simple.py`, `convenience.py`, protocols/impls, `__init__.py`

### 4.1 — HIGH · correctness · CONFIRMED (seam-break) — AsyncSession bypasses the EventQueue seam and hangs
**Landmark:** `AsyncSession._next_delay` (`aio.py`) reads `getattr(eq, "_pending", None)`,
reaching into `MemoryEventQueue`'s private field. Any other `EventQueue` impl (the seam's whole
point) returns `None`, so `run()` blocks on `wait_for(..., timeout=None)` forever and **delayed
sends never fire**. The private-field access is confirmed by reading `aio.py`; the hang is its
direct consequence. **Fix:** add an `EventQueue.time_to_next(now) -> Optional[float]` protocol
method and call that; add a test driving `AsyncSession` with a non-`MemoryEventQueue`.

### 4.2 — MEDIUM · correctness · PLAUSIBLE — viz label escaping incomplete
**Landmark:** only DOT *edge* labels escape quotes; `_dot_node` (node/cluster labels) and
`_mermaid_state` (history label) emit `node.id` raw, and nothing escapes `\` or newlines
anywhere in `viz.py`. A state id/event containing `"` breaks the output. **Fix:** one `_escape()`
helper on every quoted label; add a test with a quote in an id.

### 4.3 — MEDIUM · correctness · PLAUSIBLE — read operations mutate the store
**Landmark:** `NormalizedDataModel._entity` (`store.py`) uses `setdefault(...).setdefault(...)`,
so the read paths `get`/`as_data` materialise phantom empty entity dicts — evaluating a guard
mutates `store["db"]`. `resolve_actors` correctly uses read-only `.get`. **Fix:** a read-only
`_entity` for get/as_data, a separate `_ensure_entity` for writes; assert `as_data` leaves the
store untouched.

### 4.4 — MEDIUM · testing · PLAUSIBLE — async runtime under-covered
**Landmark:** `test_aio.py` never exercises `wait_stopped()`/`_stopped`, never awaits/asserts
clean shutdown after `task.cancel()`, has no delayed-send *cancellation* test and no
non-`MemoryEventQueue` test (which is why §4.1 is invisible). **Fix:** add those three.

### 4.5 — LOW — assorted
- **testing** — `test_store.py` untested surface: `transact` `KeyError` on unbound alias,
  `assoc_ident`, `AssignOp`/`DeleteOp` via `NormalizedDataModel.transact`, all-aliases branch,
  unbound-actor `resolve_actors`.
- **simplification** — `_mermaid_transitions` (`viz.py`) has a dead ternary
  (`_safe(tgt) if not c.is_final(tgt) else _safe(tgt)`) and a duplicated append.
- **organization** — near-identical drain loops in `Session._drain_delayed` (`simple.py`) and
  `AsyncSession._drain_due` (`aio.py`); extract a shared helper.
- **correctness (fragile)** — `MemoryEventQueue.tick` (`event_queue.py`) dedups by `id(e)`;
  prefer the existing monotonic `_seq`.
- **correctness** — `Session.data`/`AsyncSession.data` return a shallow `dict(...)`; nested
  normalized-store tables stay shared. Deep-copy or document as a view.
- **nits** — `resolve_aliases` redundant `not startswith("__") and not startswith("_")`;
  `EventQueue.send`/`tick` type `sendid: str = None` (should be `Optional[str]`); `__init__.__all__`
  exports both the `store` module and four of its members.

**Clean:** the four-seam design is coherent, `viz` already builds via list+`join`, the facades
are thin in a good way, and the public API in `__init__.py` is well-shaped. The one structural
crack is §4.1.

---

## 5. Test suite & gate
`run_tests.py`, `tests/**`, `tests/w3c/runner.py`, `bench/**`, `.github/workflows/ci.yml`

### 5.1 — HIGH · runner-gate · CONFIRMED — the Postgres backend is un-run behind the merge gate
**Landmark:** CI (`.github/workflows/ci.yml`) runs `pip install -e ".[dev]"` (= `pytest` only —
confirmed: no `postgres` extra, no `services:` block, no `DATABASE_URL`). So `_PG_OK` is
`False` and all 11 `test_pg_*` take the `if not _PG_OK: return _skip(...)` early return. Under
`run_tests.py`, `_skip` is a no-op returning `None` → **counted PASS while asserting nothing**
(an F2 violation). The 312-LOC Postgres backend (SKIP LOCKED claims, lease expiry,
at-least-once redelivery) has **zero execution behind the gate**. **Fix:** a dedicated CI job
with a `postgres:` service + `pip install -e ".[dev,postgres]"`; make `_skip` under
`run_tests.py` print a visible `SKIP` and track a skip count so no-op passes are distinguishable.

### 5.2 — MEDIUM · runner-gate · PLAUSIBLE — W3C gate is a count-floor with 3 tests of slack
**Landmark:** `run_all` / `--min` in `tests/w3c/runner.py` — the gate is `npass < floor`
(153 vs floor 150). A change flipping one currently-passing id PASS→FAIL is masked if any
INCOMPLETE/ERROR case flips →PASS in the same change. **Fix:** pin an expected-PASS *set* (or
allowed-FAIL list) and fail on any previously-passing regression, independent of the total.

### 5.3 — MEDIUM · runner-gate · CONFIRMED (static) — `run_tests.py` aborts the whole run on an import error
**Landmark:** `main` in `run_tests.py` calls `load_module(...)` *outside* the per-test
`try/except`, so a test file that errors on **import** stops discovery — every later file is
silently never executed. It exits non-zero (not a false green) but the summary under-reports.
It also discovers only top-level `test_*`, diverging from `pytest`. **Fix:** wrap `load_module`
in the same `try/except`, record an import failure as a FAIL, continue.

### 5.4 — MEDIUM · coverage-gap · CONFIRMED — `ecma.py` and `scxml/loader.py` have no dedicated tests
**Landmark:** both (490 LOC combined) are exercised only indirectly via the W3C runner and the
21-case smoke subset. No test names an `EcmaError` path or asserts a specific
`UnsupportedConstruct`, or that a hand-written SCXML loads to an expected structure. **Fix:**
add `test_ecma.py` and `test_scxml_loader.py`. (Compounds §3.1/§3.2 — the exploitable code is
also the least directly tested.)

### 5.5 — LOW · test-quality — assertion-free / dead fragments
**Landmark:** `test_choice_decision_state` (`test_eventless_and_final.py`) builds `s2`, sends to
it, comments "still rejected here" — but has **no assertion on `s2`** (that fragment cannot
fail). `test_datamodel_initialized_on_entry` (`test_basic.py`) builds a `chart` immediately
overwritten before use. `test_w3c_representative_subset` raises on the first failing id, masking
the rest. **Fix:** assert `s2`, delete the dead build, loop-collect W3C failures.

**Coverage map** (src module → dedicated test file):

| src module | dedicated test |
|---|---|
| `durable.py` | `test_durable.py` — excellent (real crashes, atomicity, poison/gate) |
| `durable_postgres.py` | `test_durable_postgres.py` — **dormant in CI** (§5.1) |
| `algorithm.py` | none dedicated — behavioural via basic/parallel/semantics/history/eventless |
| `scxml/loader.py` | **NONE** (§5.4) |
| `ecma.py` | **NONE** (§5.4) |
| `chart.py` | `test_validation.py` (validation only) |
| `store.py` / `viz.py` / `aio.py` / `events.py` | `test_store` / `test_viz` / `test_aio` / `test_events` ✓ |
| `invocations.py` | `test_invoke_native.py` (native; XML invoke via W3C) |
| `event_queue.py` | `test_delayed.py` (partial) |
| `elements.py`, `simple.py`, `convenience.py`, `environment.py`, `data_model.py`, `ops.py`, `working_memory.py`, `execution_model.py`, `protocols.py` | none dedicated — indirect |

**Suite performance:** no egregious slowness. `test_aio.py` uses bounded real-clock sleeps
(~0.2s total). `bench/run_bench.py` is a genuine throughput harness (warm-up self-check, three
stress shapes, `--scale` sweep), guarded by `test_bench_smoke.py`; not wired into CI, which is
acceptable for a diagnostic baseline.

---

## Recommended follow-up tickets

File one at a time (RULES D6). Suggested `[type]` + `area:*`; HIGH items first.

| # | Finding | Suggested ticket | area |
|---|---|---|---|
| 1 | §1.1 datamodel re-init on entry | ✅ **SHIPPED — #38** (persist `dm_initialized`, in-memory + durable, with regressions); late-binding guard **#39** | `area:algo` |
| 2 | §3.1 ecma RCE | `[feat]` replace `eval` with an allowlisted AST evaluator + red test | `area:scxml` |
| 3 | §3.2 billion-laughs DoS | `[feat]` reject DTD/entity decls in the loader + document trust boundary | `area:scxml` |
| 4 | §5.1 Postgres dormant in CI | `[infra]` CI Postgres service job + visible SKIP accounting | `area:durable` |
| 5 | §4.1 AsyncSession seam hang | `[feat]` `EventQueue.time_to_next` protocol method + non-Memory test | `area:model` |
| 6 | §2.1 single-worker session inversion | fold into **#35** (note the single-worker case) | `area:durable` |
| 7 | §3.3 `js_to_py` string corruption | `[feat]` AST-node rewrites instead of source substitution | `area:scxml` |
| 8 | §4.3 store mutate-on-read | `[feat]` read-only `_entity` for get/as_data | `area:model` |
| 9 | §4.2 viz escaping | `[feat]` `_escape()` on all quoted labels + test | `area:viz` |
| 10 | §5.2 W3C count-floor | `[test]` pin expected-PASS set / allowed-FAIL list | `area:scxml` |
| 11 | §5.3 `run_tests.py` import abort | `[infra]` wrap `load_module`, count import failures | `area:xcc` |
| 12 | §5.4 ecma/loader untested | `[test]` `test_ecma.py` + `test_scxml_loader.py` | `area:scxml` |
| 13 | §2.4 store duplication | `[feat]` extract `_Store` base | `area:durable` |
| 14 | §1.2–1.3 core hot-path perf | `[perf]` memoise transition domain; precompute atomic/compound sets | `area:algo` |
| — | §2.2, §2.5–2.8, §3.4–3.7, §4.4–4.5, §5.5 | lower-priority; batch or attach to the above | — |

Cross-references: **#35** (Postgres per-session FIFO), **#36** (Postgres dead-letter cap) —
already open; §2.1 and §2.6 feed their designs.
