# Improvement areas for `statecharts-py` — research findings

> Research spike for [#1](https://github.com/avidrucker/statecharts-py/issues/1).
> First pass, ~90 min time box. This is a **findings doc**, not an implementation plan —
> each actionable item below should become its own follow-up ticket.
>
> Grounding facts about our own engine are cited as `file:line`; external claims carry a
> URL; prior-art claims cite `fulcrologic/statecharts` issues or its docs.

## Headline

The single most important finding: **`statecharts-py` is, in several concrete places,
*more* W3C-conformant than the Clojure library it ports.** The port enforces
system-variable read-only semantics, implements `error.communication`, populates
`_ioprocessors`, supports late binding, and defaults to strict block-abort error
semantics — all of which the upstream `fulcrologic/statecharts` either skips or makes
opt-in (see [§Prior art](#prior-art-tony-kays-open-questions)). So the "room for
improvement" here is **not** "catch up to upstream." It is: (a) prove and defend the
correctness we claim, (b) understand our performance envelope, and (c) close a small set
of genuinely-undefined corners *only if* real use demands it.

---

## RQ1 — Performance

### What we found

No benchmark harness exists in the repo today (`grep` for `benchmark`/`timeit`/`perf`
finds nothing under `tests/`). We are flying blind on the performance envelope — which is
itself the top RQ1 finding: **we cannot yet answer "how big / how fast" for this engine.**

Candidate hot spots, from reading the algorithm:

1. **Per-event immutable snapshot cost.** Each processed event rebuilds fresh frozen
   collections for the new `WorkingMemory`:
   `configuration=frozenset(...)`, `history_value={k: frozenset(v) ...}`
   (`src/statecharts/algorithm.py:99`–`101`). This is O(active states + history entries)
   allocation *per event*, independent of how much actually changed. For high-throughput
   or large charts this is the most likely first bottleneck. XState hit an analogous cost
   on context mutation and its planned fix was to **cache the enabled-transition lookup**
   rather than re-walk the node tree each step
   ([xstate#3757](https://github.com/statelyai/xstate/discussions/3757)).
2. **Transition selection re-walks the chart each microstep.** The optimal-enabled-set
   computation and LCCA/exit-set work run every microstep with no memoization across
   steps. Same shape as the XState finding above — a precomputed per-state transition
   index is the known mitigation.
3. **Document-order indexing** is built once at chart-compile time (good), so it is *not*
   a per-event cost — worth confirming it stays that way.

### Recommendation

- File a ticket to **add a micro-benchmark harness** (stdlib `timeit`/`perf_counter`,
  zero deps to match the project's zero-dep stance): a wide parallel chart, a deep
  compound chart, and a high-event-count loop, reporting events/sec and allocations.
  Without this, every other perf claim is speculation.
- Only *after* numbers exist, consider (a) diffing configurations instead of rebuilding
  frozensets, and (b) a per-state transition index. Do not optimize pre-measurement.

---

## RQ2 — Undefined / underspecified behavior

The 3 known non-green W3C tests (`why-98-percent-passing.md`) are all at the
expression-language / spec-undefined edge, **not** the engine:

| Gap | W3C test | Disposition | Rationale |
|---|---|---|---|
| Inline `function(){...}` IIFE in a guard | `test224` | **Document as out-of-scope; revisit only via ExecutionModel swap** | Needs a full JS statement interpreter. Upstream doesn't do `<script>` flow control either — faithful to skip. Cheapest fix is dropping a sandboxed JS evaluator (`quickjs`/`js2py`) behind the `ExecutionModel` seam, flipping ~4 tests at once with no engine change. |
| Inline `<scxml>` as a data *value* | `test530` | **Won't-do (low value)** | Needs SCXML docs as first-class runtime values threaded assign→expr→invoke. Upstream also skips this (its bulk inline-invoke suite, incl. 530, is unsupported — see `_skipped.md`). |
| Cancel a delayed event in *another* session | `test207` | **Won't-do (spec-undefined)** | The SCXML spec itself says "there is no defined way to refer to an event in another process." Testing genuinely-undefined behavior. |

**Newly surfaced corners** (from comparing our source against upstream's skip list — areas
where behavior is *defined by us* but may be thinly tested):

- **`error.communication` timing.** We implement it (`algorithm.py:665`, `692`–`693`,
  `invocations.py:95`) and deliberately make it *async, non-block-aborting* — the opposite
  of `error.execution` (`algorithm.py:692`). Upstream does **not** implement
  `error.communication` at all (its W3C `test496` is skipped). This is *our* semantics to
  defend; it deserves an explicit test asserting the async-vs-abort distinction.
- **`_ioprocessors` / `_event.origin` / `origintype` population.** We populate
  `_ioprocessors` (`algorithm.py:92`). Upstream skips the whole family (tests 325, 326,
  336, 349, 352, 500, 501). Worth auditing whether our `_event.origin`/`origintype` are
  routable — if not, that's an undefined corner for round-trip sends.
- **Strict block-abort is our *default*.** Executable-content errors abort the rest of the
  block (`algorithm.py:565`). Upstream defaults to *lenient* and makes strict opt-in
  (`(simple/strict-env)`, Conformance.adoc §"Strict block-error semantics"). Same words,
  opposite default — a real behavioral divergence a Clojure→Python porter would trip on.
  Should be called out prominently in our docs.

### Recommendation

Ticket: an **"undefined-behavior register"** doc section + targeted tests pinning our
*chosen* semantics for `error.communication`, block-abort default, and system-var
enforcement — so these are defended behaviors, not accidents.

---

## RQ3 — Correctness edge cases the W3C suite does not cover

The IRP suite is thorough but has blind spots. At-risk areas and their current coverage:

| Edge case | Currently tested? | Risk |
|---|---|---|
| Deep history **inside** parallel regions re-entered after partial exit | `tests/test_history.py` + `test_parallel.py` exist but not obviously the *crossed* case | Medium — history×parallel is the classic bug nest |
| Same transition enabled from two parallel regions (dedup) | Fixed + noted in README ("optimally enabled set must be a set") | Low — regression-guarded |
| `error.execution` **ordering** vs `done.*` events (upstream skips 488/528/312 family) | Unclear | Medium — ordering bugs are silent |
| System-variable write rejection actually raising (we enforce it, upstream doesn't) | Enforcement code at `algorithm.py:611`–`612`; test not obvious | Medium — assert the raise |
| Late binding (`_binding="late"`, `algorithm.py:125`–`131`) — we support it, upstream doesn't | Not obviously covered | Medium — untested feature = latent bug |

We have **13 native test files** (`tests/test_*.py`) plus the W3C smoke guard — solid, but
the five rows above are the gaps worth closing with focused tests.

### Recommendation

Ticket: **targeted edge-case tests** for history×parallel crossing, error/done ordering,
system-var raise, and late binding. These are cheap and pin behaviors the W3C suite leaves
implicit.

---

## RQ4 — API / ergonomics

The four-seam design (`DataModel` / `ExecutionModel` / `EventQueue` / `algorithm`) is the
port's strongest asset and maps cleanly onto upstream's protocol split. Comparison:

| Concern | `statecharts-py` | `fulcrologic/statecharts` | Apache Commons SCXML | XState |
|---|---|---|---|---|
| Data storage | `DataModel` protocol (swappable) | `DataModel` protocol | XML data tree (fixed) | JS context object |
| Expression eval | `ExecutionModel` (native callables + ecma subset) | lambda execution model | JEXL / limited | JS (native) |
| Event delivery | `EventQueue` (memory / SQLite durable) | protocol + basic impls | internal only | actor mailbox |
| Distribution | SQLite → Postgres path (documented) | none built-in | none | actor model |

Ergonomic gaps observed (no external cite — these are direct-observation candidates):

- No **benchmark / profiling** entry point (see RQ1).
- No published **migration note for Clojure users** documenting the default-semantics
  divergences found in RQ2 (strict-by-default, system-var enforcement). This is a real
  ergonomic trap for the port's most likely early adopters.
- Consider a convenience for the "swap a real JS evaluator behind `ExecutionModel`"
  path, since that is the documented escape hatch for the 3 SKIP tests.

Apache Commons SCXML is a cautionary comparison: last release 0.9 in 2008, still chasing
W3C alignment for a "2.0" that hasn't landed, hampered by an XML-data-tree model that
fights ECMAScript
([commons.apache.org/scxml/roadmap](https://commons.apache.org/scxml/roadmap.html)). Our
pluggable-`DataModel` choice is precisely what avoids that trap — worth stating as a
design win.

### Recommendation

Ticket (docs): a **"Porting from Clojure / choosing your seams" guide** covering the
default-semantics divergences and the ExecutionModel-swap pattern.

---

## RQ5 — Durability / distribution

Our durable layer is SQLite-backed with a documented Postgres port path
(`SELECT ... FOR UPDATE SKIP LOCKED`, per README). Research into that pattern surfaces
concrete, well-known risks to plan for **before** the Postgres port:

- **`SKIP LOCKED` is the right primitive** — it lets N workers pull disjoint work without
  contention ([dbpro SKIP LOCKED](https://www.dbpro.app/blog/postgresql-skip-locked),
  [netdata](https://www.netdata.cloud/academy/update-skip-locked/)). Temporal itself uses
  `SELECT ... FOR UPDATE SKIP LOCKED` for task dispatch
  ([backend.how Temporal](https://backend.how/posts/temporal-under-the-hood/)).
- **Polling doesn't scale like push.** A poll loop adds DB traffic; the standard
  mitigation is a **composite index on `(status, next_fire_time)`** (or `(status,
  created_at)`) so "find the oldest due job" is an index scan, not a table scan
  ([vrajat](https://vrajat.com/posts/postgres-queue-skip-locked-unlogged/)). Our schema
  should be audited for exactly this index on the timer/queue table.
- **Crash-recovery model.** Temporal replays event history against workflow code
  ([backend.how](https://backend.how/posts/temporal-under-the-hood/)); simpler engines
  like Armin Ronacher's "Absurd" are *just a queue + state store in Postgres*, no replay
  ([lucumr Absurd Workflows](https://lucumr.pocoo.org/2025/11/3/absurd-workflows/)). Our
  "only JSON-able working memory + pending timers are persisted" model is the Absurd
  school — worth stating explicitly that we do **not** do deterministic replay, and what
  that implies for non-deterministic executable content across a restart.

### Recommendation

Ticket (research/design): **Postgres port readiness** — confirm the composite index,
decide and document the crash-recovery contract (no-replay + idempotency expectations),
and note the multi-node story.

---

## Prior art: Tony Kay's open questions

`fulcrologic/statecharts` has **0 open GitHub issues** (`gh api repos/fulcrologic/statecharts`,
2026-06-26 push, 123★) and no Discussions. His "open questions" therefore live in his
**docs and closed-issue history**, not an issue tracker. Extracted:

**Documented intentional deviations / tensions** (from `Conformance.adoc`):

- **Document-order ambiguity** — the spec is "vague about Document Order"; upstream
  defaults to depth-first but offers breadth-first, visible only in deeply-nested parallel
  nodes. *Transfers directly:* we should confirm which we implement and whether we expose
  the choice.
- **System-var read-only NOT enforced** — upstream skips W3C 322/324/325/326/329/346
  because its pluggable `DataModel` "has no general way to intercept writes." **We took the
  opposite call and enforce it** (`algorithm.py:611`). This is the sharpest open design
  question that transfers: *is enforcement the right call given the same pluggable-model
  abstraction, or does it leak the abstraction?*
- **Strict block-error semantics opt-in** — upstream keeps this opt-in for backward compat;
  we default it on. *Transfers:* document the divergence.
- **Intentionally unsupported:** inline `<invoke><content><scxml>`, late binding (early
  only), top-level load-time `<script>`, BasicHTTP I/O processor, external XML chart
  loading. *We match on inline-invoke-content, `<script>`, HTTP; we diverge by
  additionally supporting late binding and native `<invoke>`.*

**Recurring problem areas** (from closed issues — the questions that *keep coming back*):

- [#28](https://github.com/fulcrologic/statecharts/issues/28) — "Events match on substrings
  instead of tokens." Exactly the dotted-prefix/token-matching bug our README says we fixed.
  Confirms it's a genuine trap; keep our regression test.
- [#23](https://github.com/fulcrologic/statecharts/issues/23) — `e/assign` running an op
  instead of associating a value. Assign semantics are a repeat offender — worth an
  explicit assign test on our side.
- [#19](https://github.com/fulcrologic/statecharts/issues/19) — "Custom executable content
  doesn't work." The extensibility seam is easy to break — a test that a *user-defined*
  executable element runs would guard this.
- [#20](https://github.com/fulcrologic/statecharts/issues/20),
  [#21](https://github.com/fulcrologic/statecharts/issues/21),
  [#25](https://github.com/fulcrologic/statecharts/issues/25),
  [#27](https://github.com/fulcrologic/statecharts/issues/27),
  [#30](https://github.com/fulcrologic/statecharts/issues/30),
  [#33](https://github.com/fulcrologic/statecharts/issues/33) — all Fulcro **routing** /
  URL-sync issues. *Do not transfer* — they belong to his Fulcro UI integration, which the
  port does not (yet) have.

---

## Prioritized opportunities (Impact × Effort)

| # | Opportunity | Impact | Effort | Source |
|---|---|---|---|---|
| 1 | Benchmark harness (events/sec, wide/deep/loop) — unblocks all perf work | High | Med | RQ1 |
| 2 | Edge-case tests: history×parallel, error/done ordering, system-var raise, late binding | High | Low | RQ3 |
| 3 | "Undefined-behavior register" + tests pinning `error.communication`/block-abort/system-var semantics | High | Low | RQ2 |
| 4 | "Porting from Clojure / choosing your seams" doc (default-semantics divergences) | Med | Low | RQ4, prior art |
| 5 | Postgres port readiness: composite index audit + crash-recovery contract | Med | Med | RQ5 |
| 6 | Per-event snapshot / transition-index perf work (only after #1 shows a problem) | Med | High | RQ1 |
| 7 | Sandboxed JS evaluator behind `ExecutionModel` to flip the 3 SKIP + `test224` | Low | High | RQ2 |
| 8 | Audit `_event.origin`/`origintype` routability for round-trip sends | Low | Med | RQ2 |

## Shortlist of follow-up tickets to file

1. **[perf] Add a zero-dep micro-benchmark harness** (opportunity #1). *Do this first.*
2. **[test] Edge-case tests for history×parallel, error/done ordering, system-var raise,
   late binding** (opportunity #2).
3. **[docs] Undefined-behavior register + semantics-pinning tests** (opportunity #3).
4. **[docs] "Porting from Clojure" divergence guide** (opportunity #4).
5. **[research] Postgres port readiness** (opportunity #5).

Opportunities #6–#8 are deliberately *not* filed yet — #6 is gated on #1's numbers, and
#7/#8 are low-value corners already tracked in `current-wont-dos.md`.

---

### Sources

- [statelyai/xstate discussion #3757 — context-mutation performance](https://github.com/statelyai/xstate/discussions/3757)
- [Apache Commons SCXML roadmap (2.0 W3C-alignment gaps)](https://commons.apache.org/scxml/roadmap.html)
- [W3C SCXML recommendation](https://www.w3.org/TR/scxml/)
- [alexzhornyak SCXML framework conformance table](https://alexzhornyak.github.io/SCXML-tutorial/Tests/)
- [PostgreSQL FOR UPDATE SKIP LOCKED job queue (dbpro)](https://www.dbpro.app/blog/postgresql-skip-locked)
- [FOR UPDATE SKIP LOCKED for queue workflows (Netdata)](https://www.netdata.cloud/academy/update-skip-locked/)
- [Postgres queue with SKIP LOCKED + composite index (vrajat)](https://vrajat.com/posts/postgres-queue-skip-locked-unlogged/)
- [Temporal under the hood (SKIP LOCKED task dispatch, replay)](https://backend.how/posts/temporal-under-the-hood/)
- [Absurd Workflows — durable execution with just Postgres (Armin Ronacher)](https://lucumr.pocoo.org/2025/11/3/absurd-workflows/)
- `fulcrologic/statecharts`: `Conformance.adoc`, `CHANGELOG`, `src/test/.../irp/_skipped.md`, issues [#19](https://github.com/fulcrologic/statecharts/issues/19), [#20](https://github.com/fulcrologic/statecharts/issues/20), [#21](https://github.com/fulcrologic/statecharts/issues/21), [#23](https://github.com/fulcrologic/statecharts/issues/23), [#25](https://github.com/fulcrologic/statecharts/issues/25), [#27](https://github.com/fulcrologic/statecharts/issues/27), [#28](https://github.com/fulcrologic/statecharts/issues/28), [#30](https://github.com/fulcrologic/statecharts/issues/30), [#33](https://github.com/fulcrologic/statecharts/issues/33)
- Local engine: `src/statecharts/algorithm.py`, `invocations.py`, `why-98-percent-passing.md`, `current-wont-dos.md`
