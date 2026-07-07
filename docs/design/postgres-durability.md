# Postgres durability port — readiness design

Research/design spike for [#6](https://github.com/avidrucker/statecharts-py/issues/6).
**No implementation here** — this pressure-tests the "ports directly to Postgres" claim in
`durable.py` and pins the decisions (index, claim atomicity, crash-recovery contract) a
`PostgresStore` must get right, before anyone writes it.

## Current state (SQLite)

`durable.py` persists two tables — `sessions` (working memory as JSON) and `timers` (the
durable mailbox: `id, session_id, due, event, sendid`) — with indexes `timers_due(due)` and
`timers_session(session_id)`. Delivery (`SqliteStore.claim_due` + `DurableRuntime.tick`):

1. `claim_due(now)` — `BEGIN IMMEDIATE`, `SELECT ... WHERE due<=? ORDER BY due, id`, then
   `DELETE WHERE id=?` for each row, **commit**. One exclusive write txn → safe for multiple
   processes on one machine.
2. `tick` then calls `_deliver` per claimed event: load session, `process_event`,
   `save_session` (persist WM) — **in a separate transaction**.

The docstring says the schema/queries "port directly to Postgres … replace `claim_due`'s
transaction with `SELECT ... FOR UPDATE SKIP LOCKED`." That's directionally right, but the
port has three real decisions to make first.

## RQ1 — Indexing under `FOR UPDATE SKIP LOCKED`

The claim query is `WHERE due <= now ORDER BY due, id`. `SKIP LOCKED` is the correct
primitive: it lets N workers pull disjoint due rows without the contention where most
workers block on the same head-of-queue rows
([dbpro](https://www.dbpro.app/blog/postgresql-skip-locked),
[netdata](https://www.netdata.cloud/academy/update-skip-locked/)). Temporal dispatches tasks
exactly this way ([backend.how](https://backend.how/posts/temporal-under-the-hood/)).

**Finding:** `timers_due(due)` alone supports the range scan but not the full sort (the `id`
tiebreaker forces a heap step). The standard guidance is a composite index on the columns
used to *find work* so "oldest due first" is an index-only ordered scan
([vrajat](https://vrajat.com/posts/postgres-queue-skip-locked-unlogged/)).

**Recommendation:** index `timers(due, id)` (replacing `timers_due`). If RQ2's status
column is added, make it a **partial** index `WHERE status = 'ready'` on `(due, id)` so
locked/in-flight rows don't bloat the scan.

## RQ2 — Claim atomicity (the crash window)

**Finding — this is the sharpest one.** `claim_due` **deletes** the timer and commits;
`_deliver` then processes and persists WM in a *separate* transaction (`durable.py` `tick`
→ `_deliver`). A crash **between** the delete-commit and the `save_session`-commit loses the
event: the timer is gone, but its effect (the new working memory) was never written. That is
**at-most-once with a data-loss window**, not the at-least-once most workflow users expect.
This already exists on SQLite (filed as its own bug,
[#21](https://github.com/avidrucker/statecharts-py/issues/21)); a naive Postgres port
inherits it.

Two ways to close it:

- **(a) One transaction spanning claim + persist.** For the multi-worker Postgres case this
  is undesirable: `process_event` is engine/app code that may run for a while, and holding a
  row lock across it limits throughput and couples the queue to processing time. **But for
  the single-process SQLite backend it is likely the *simplest* fix** — wrap claim +
  `process_event` + `save_session` in one write transaction, no lease machinery needed.
- **(b) Visibility-timeout (lease) column — recommended.** Don't delete on claim; set
  `status='in_flight', claimed_at=now` under `FOR UPDATE SKIP LOCKED`. On successful
  `save_session`, delete the row. A worker that crashes leaves an in-flight row whose lease
  expires (`claimed_at < now - lease`), so another worker re-claims it → **at-least-once**.
  This is the Temporal-style model ([backend.how](https://backend.how/posts/temporal-under-the-hood/)).

At-least-once means **handlers must be idempotent** (see RQ3).

## RQ3 — Crash-recovery contract

We persist only JSON-able working memory + pending timers — there is **no deterministic
replay**. This is the "Absurd" school (a queue + a state store in Postgres, no event-history
replay — [Armin Ronacher](https://lucumr.pocoo.org/2025/11/3/absurd-workflows/)), *not*
Temporal's replay-workflow-code model ([backend.how](https://backend.how/posts/temporal-under-the-hood/)).

**Contract to document and enforce:**

1. **Delivery is at-least-once** (with RQ2(b)); design handlers to tolerate a duplicate
   delivery of the same event.
2. **Non-deterministic executable content is re-run, not replayed.** If a guard/action does
   I/O, reads the wall clock, or calls a RNG, a redelivery re-executes it. Side-effecting
   actions must be **idempotent** or externally deduplicated.
3. **The unit of progress is `(process_event → save_session)`.** Only committed working
   memory survives a restart; an in-flight step that didn't commit is retried from the last
   committed WM.

## RQ4 — Multi-node

`SKIP LOCKED` gives N workers disjoint work with no coordination
([dbpro](https://www.dbpro.app/blog/postgresql-skip-locked)). Notes:

- **No global ordering across nodes.** Per-session ordering holds only if a session is not
  processed concurrently by two workers — add a per-session advisory lock (or claim by
  `session_id`) if a session's events must stay serialized.
- **Polling vs push.** The current tick loop polls; polling adds DB load that a composite
  index (RQ1) keeps cheap, and `LISTEN/NOTIFY` can later cut idle polling
  ([vrajat](https://vrajat.com/posts/postgres-queue-skip-locked-unlogged/)).
- **`SELECT ... FOR UPDATE SKIP LOCKED` requires a real transaction per claim** (autocommit
  off), unlike SQLite's `BEGIN IMMEDIATE`.

## Recommendation summary

| Decision | Recommendation |
|---|---|
| Index | `timers(due, id)` (partial `WHERE status='ready'` if RQ2(b) lands) |
| Claim | RQ2(b) lease/visibility-timeout column, not delete-on-claim |
| Delivery contract | at-least-once + idempotent handlers; no deterministic replay |
| Ordering | per-session advisory lock if serialized per-session delivery is required |
| Queries | `FOR UPDATE SKIP LOCKED` in a per-claim transaction (autocommit off) |

## Follow-up DEV ticket (to file)

**`[perf/feat] PostgresStore: durable store backend with SKIP LOCKED lease delivery`**
- Add a `PostgresStore` mirroring the `SqliteStore` interface (`save_session`/`load_session`/
  `enqueue`/`cancel`/`claim_due`/`next_due`).
- Schema: `timers(due, id)` index + `status`/`claimed_at` lease columns per RQ2(b).
- `claim_due`: `SELECT ... WHERE due<=now AND status='ready' ORDER BY due, id FOR UPDATE SKIP LOCKED`, mark in-flight, delete on successful persist, lease-expiry re-claim.
- Document the at-least-once + idempotency contract (RQ3) in the durable module and the
  [behavior register](../reference/behavior-register.md).
- Tests: a fake two-worker race (disjoint claims), a crash-between-claim-and-persist
  redelivery, and lease expiry.

## Sources

- [PostgreSQL FOR UPDATE SKIP LOCKED (dbpro)](https://www.dbpro.app/blog/postgresql-skip-locked)
- [FOR UPDATE SKIP LOCKED for queue workflows (Netdata)](https://www.netdata.cloud/academy/update-skip-locked/)
- [Postgres queue with SKIP LOCKED + composite index (vrajat)](https://vrajat.com/posts/postgres-queue-skip-locked-unlogged/)
- [Temporal under the hood — SKIP LOCKED dispatch + replay (backend.how)](https://backend.how/posts/temporal-under-the-hood/)
- [Absurd Workflows — durable execution with just Postgres (Armin Ronacher)](https://lucumr.pocoo.org/2025/11/3/absurd-workflows/)
- Current implementation: `src/statecharts/durable.py`
